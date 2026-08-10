"""Reset-only installer for the clean conversation-kernel migration universe."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from hashlib import sha256

from psycopg import Connection

from pulsara_agent import __version__
from pulsara_agent.storage.migrations.contracts import (
    BASELINE_NAME,
    PostgresMigrationLedgerRowFact,
    UNIVERSE_GENERATION,
    UNIVERSE_ID,
    canonical_utc,
)
from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)
from pulsara_agent.storage.migrations.grants import PostgresRuntimeGrantExecutor
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS
from pulsara_agent.storage.migrations.registry import (
    POSTGRES_MIGRATION_REGISTRY,
    PostgresMigrationRegistry,
)
from pulsara_agent.storage.postgres_endpoint import ResolvedPostgresConnectionFactory


_LOCK_KEY = int.from_bytes(
    sha256(b"pulsara:clean-migration-universe:v1").digest()[:8],
    byteorder="big",
    signed=True,
)

@dataclass(frozen=True, slots=True)
class PostgresDatabaseIdentity:
    database_name: str
    database_oid: int
    runtime_role: str
    normalized_search_path: tuple[str, ...]
    server_version_num: int


@dataclass(frozen=True, slots=True)
class PostgresMigrationReport:
    status: str
    database_name: str
    runtime_role: str
    previous_head_version: int | None
    migration_head_version: int
    applied_versions: tuple[int, ...]
    added_grant_fingerprints: tuple[str, ...]
    registry_prefix_fingerprint: str
    universe_id: str
    universe_generation: int
    universe_fingerprint: str
    baseline_commit_confirmation: str
    report_fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class BaselineCommitConfirmation(StrEnum):
    FULL = "FULL"
    NONE = "NONE"
    CONFLICT = "CONFLICT"


class _BaselineCommitOutcomeUnknown(RuntimeError):
    pass


class PostgresMigrationRunner:
    def __init__(
        self,
        *,
        admin_dsn: str,
        runtime_dsn: str,
        registry: PostgresMigrationRegistry = POSTGRES_MIGRATION_REGISTRY,
        application_version: str = __version__,
        grant_executor: PostgresRuntimeGrantExecutor | None = None,
    ) -> None:
        if not admin_dsn.strip():
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.ADMIN_DSN_REQUIRED,
                "PULSARA_POSTGRES_ADMIN_DSN is required for db migrate",
            )
        self._admin = ResolvedPostgresConnectionFactory(
            admin_dsn, application_name="pulsara-clean-schema-admin"
        )
        self._runtime = ResolvedPostgresConnectionFactory(
            runtime_dsn, application_name="pulsara-clean-schema-runtime-probe"
        )
        if self._admin.endpoint.endpoint_fingerprint != self._runtime.endpoint.endpoint_fingerprint:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.DATABASE_IDENTITY_MISMATCH,
                "admin and runtime DSNs must resolve to the same database target",
            )
        self._registry = registry
        self._application_version = application_version
        self._grant_executor = grant_executor or PostgresRuntimeGrantExecutor()

    def migrate(self, *, deadline_monotonic: float) -> PostgresMigrationReport:
        return self._migrate(
            deadline_monotonic=deadline_monotonic,
            allow_none_retry=True,
        )

    def _migrate(
        self,
        *,
        deadline_monotonic: float,
        allow_none_retry: bool,
    ) -> PostgresMigrationReport:
        self._registry.verify_resources()
        definition = self._registry.definition(0)
        runtime_identity = self._read_runtime_identity(deadline_monotonic)
        try:
            with self._admin.connect(
                deadline_monotonic=deadline_monotonic, autocommit=False
            ) as connection:
                identity = _read_identity_from_connection(connection)
                _require_same_database(identity, runtime_identity)
                connection.execute(
                    "SELECT pg_catalog.pg_advisory_xact_lock(%s)", (_LOCK_KEY,)
                )
                rows = read_migration_ledger(connection)
                if rows is not None:
                    _validate_clean_ledger(rows, definition)
                    _verify_vector_capability(connection)
                    _verify_product_relations(connection)
                    _verify_runtime_grants(
                        connection, runtime_identity.runtime_role
                    )
                    connection.rollback()
                    return _report(
                        identity=runtime_identity,
                        applied=(),
                        grants=(),
                        previous=0,
                        confirmation="FULL",
                        registry=self._registry,
                    )

                _require_empty_pulsara_world(connection)
                _require_compatible_vector_if_present(connection)
                try:
                    connection.execute(definition.resource_text())
                    _verify_vector_capability(connection)
                    connection.execute(
                        """
                        INSERT INTO public.pulsara_schema_migrations (
                            universe_id, universe_generation,
                            universe_fingerprint, version, name,
                            resource_sha256, migration_contract_fingerprint,
                            registry_prefix_fingerprint, application_version
                        ) VALUES (%s, %s, %s, 0, %s, %s, %s, %s, %s)
                        """,
                        (
                            UNIVERSE_ID,
                            UNIVERSE_GENERATION,
                            definition.identity.universe_fingerprint,
                            BASELINE_NAME,
                            definition.expected_sha256,
                            definition.migration_contract_fingerprint,
                            definition.registry_prefix_fingerprint,
                            self._application_version,
                        ),
                    )
                    grants = self._grant_executor.reconcile(
                        connection, runtime_role=runtime_identity.runtime_role
                    )
                    _verify_product_relations(connection)
                    _verify_runtime_grants(
                        connection, runtime_identity.runtime_role
                    )
                except PostgresSchemaError:
                    connection.rollback()
                    raise
                except BaseException as exc:
                    connection.rollback()
                    raise PostgresSchemaError(
                        PostgresSchemaFailureCode.MIGRATION_FAILED,
                        f"clean version-0 baseline failed: {type(exc).__name__}",
                    ) from exc
                try:
                    connection.commit()
                except BaseException as exc:
                    raise _BaselineCommitOutcomeUnknown from exc
        except _BaselineCommitOutcomeUnknown as exc:
            confirmation = self.confirm_baseline(
                deadline_monotonic=deadline_monotonic
            )
            if confirmation is BaselineCommitConfirmation.FULL:
                return _report(
                    identity=runtime_identity,
                    applied=(0,),
                    grants=(),
                    previous=None,
                    confirmation="FULL",
                    registry=self._registry,
                )
            if confirmation is BaselineCommitConfirmation.NONE and allow_none_retry:
                return self._migrate(
                    deadline_monotonic=deadline_monotonic,
                    allow_none_retry=False,
                )
            code = (
                PostgresSchemaFailureCode.MIGRATION_CONFIRMATION_CONFLICT
                if confirmation is BaselineCommitConfirmation.CONFLICT
                else PostgresSchemaFailureCode.MIGRATION_CONFIRMATION_UNRESOLVED
            )
            raise PostgresSchemaError(
                code,
                "clean baseline commit could not be confirmed safely",
            ) from exc
        return _report(
            identity=runtime_identity,
            applied=(0,),
            grants=grants,
            previous=None,
            confirmation="FULL",
            registry=self._registry,
        )

    def confirm_baseline(
        self, *, deadline_monotonic: float
    ) -> BaselineCommitConfirmation:
        runtime_identity = self._read_runtime_identity(deadline_monotonic)
        with self._admin.connect(
            deadline_monotonic=deadline_monotonic,
            autocommit=False,
        ) as connection:
            identity = _read_identity_from_connection(connection)
            _require_same_database(identity, runtime_identity)
            connection.execute(
                "SELECT pg_catalog.pg_advisory_xact_lock(%s)", (_LOCK_KEY,)
            )
            disposition = classify_clean_baseline_confirmation(
                connection,
                runtime_role=runtime_identity.runtime_role,
                registry=self._registry,
            )
            connection.rollback()
            return disposition

    def _read_runtime_identity(self, deadline: float) -> PostgresDatabaseIdentity:
        with self._runtime.connect(deadline_monotonic=deadline, autocommit=True) as connection:
            return _read_identity_from_connection(connection)


def read_migration_ledger(
    connection: Connection,
) -> tuple[PostgresMigrationLedgerRowFact, ...] | None:
    relation_row = connection.execute(
        "SELECT pg_catalog.to_regclass('public.pulsara_schema_migrations') AS relation"
    ).fetchone()
    relation = _cell(relation_row, 0, "relation")
    if relation is None:
        return None
    columns = tuple(
        _cell(row, 0, "column_name")
        for row in connection.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'pulsara_schema_migrations'
            ORDER BY ordinal_position
            """
        ).fetchall()
    )
    expected = (
        "universe_id",
        "universe_generation",
        "universe_fingerprint",
        "version",
        "name",
        "resource_sha256",
        "migration_contract_fingerprint",
        "registry_prefix_fingerprint",
        "applied_at",
        "application_version",
    )
    if columns != expected:
        raise _reset_required("database contains a legacy migration ledger")
    records = connection.execute(
        """
        SELECT universe_id, universe_generation, universe_fingerprint,
               version, name, resource_sha256, migration_contract_fingerprint,
               registry_prefix_fingerprint, applied_at, application_version
        FROM public.pulsara_schema_migrations ORDER BY version
        """
    ).fetchall()
    return tuple(
        PostgresMigrationLedgerRowFact(
            universe_id=str(_cell(row, 0, "universe_id")),
            universe_generation=int(_cell(row, 1, "universe_generation")),
            universe_fingerprint=str(_cell(row, 2, "universe_fingerprint")),
            version=int(_cell(row, 3, "version")),
            name=str(_cell(row, 4, "name")),
            resource_checksum=str(_cell(row, 5, "resource_sha256")),
            migration_contract_fingerprint=str(
                _cell(row, 6, "migration_contract_fingerprint")
            ),
            registry_prefix_fingerprint=str(
                _cell(row, 7, "registry_prefix_fingerprint")
            ),
            applied_at_utc=canonical_utc(_cell(row, 8, "applied_at")),
            application_version=str(_cell(row, 9, "application_version")),
        )
        for row in records
    )


def classify_clean_baseline_confirmation(
    connection: Connection,
    *,
    runtime_role: str,
    registry: PostgresMigrationRegistry = POSTGRES_MIGRATION_REGISTRY,
) -> BaselineCommitConfirmation:
    """Classify a lost baseline commit ACK without mutating database state."""

    try:
        rows = read_migration_ledger(connection)
        if rows is None:
            _require_empty_pulsara_world(connection)
            _require_compatible_vector_if_present(connection)
            return BaselineCommitConfirmation.NONE
        _validate_clean_ledger(rows, registry.definition(0))
        _verify_vector_capability(connection)
        _verify_product_relations(connection)
        _verify_runtime_grants(connection, runtime_role)
    except BaseException:
        return BaselineCommitConfirmation.CONFLICT
    return BaselineCommitConfirmation.FULL


def _validate_clean_ledger(rows, definition) -> None:
    if len(rows) != 1:
        raise _reset_required("migration ledger is not the clean version-0 genesis")
    row = rows[0]
    if (
        row.universe_fingerprint != definition.identity.universe_fingerprint
        or row.resource_checksum != definition.expected_sha256
        or row.migration_contract_fingerprint
        != definition.migration_contract_fingerprint
        or row.registry_prefix_fingerprint != definition.registry_prefix_fingerprint
    ):
        raise _reset_required("migration universe identity does not match this binary")


def _require_empty_pulsara_world(connection: Connection) -> None:
    from pulsara_agent.storage.migrations.manifest import (
        PULSARA_LEGACY_PUBLIC_RELATION_NAMES,
    )

    schemas = {
        _cell(row, 0, "nspname")
        for row in connection.execute(
            "SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname = 'pulsara_v3'"
        ).fetchall()
    }
    public_relations = {
        _cell(row, 0, "relname")
        for row in connection.execute(
            """
            SELECT c.relname FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','S')
              AND NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_depend d
                JOIN pg_catalog.pg_extension e ON e.oid = d.refobjid
                WHERE d.classid = 'pg_catalog.pg_class'::regclass
                  AND d.objid = c.oid
                  AND d.refclassid = 'pg_catalog.pg_extension'::regclass
                  AND d.deptype = 'e'
              )
            """
        ).fetchall()
    }
    legacy_relations = public_relations & PULSARA_LEGACY_PUBLIC_RELATION_NAMES
    if schemas or legacy_relations:
        raise _reset_required(
            "database contains Pulsara-owned objects without clean ledger"
        )


def _require_compatible_vector_if_present(connection: Connection) -> None:
    row = connection.execute(
        """
        SELECT n.nspname, e.extversion FROM pg_catalog.pg_extension e
        JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
        WHERE e.extname = 'vector'
        """
    ).fetchone()
    if row is None:
        return
    if _cell(row, 0, "nspname") != "public":
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.CATALOG_DRIFT,
            "vector extension exists outside public schema",
        )
    if _version_tuple(str(_cell(row, 1, "extversion"))) < (0, 5, 0):
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.EXTENSION_TOO_OLD,
            "public.vector must be at least version 0.5.0",
        )


def _verify_vector_capability(connection: Connection) -> None:
    _require_compatible_vector_if_present(connection)
    row = connection.execute(
        "SELECT pg_catalog.to_regtype('public.vector')::text AS vector_type"
    ).fetchone()
    if row is None or _cell(row, 0, "vector_type") is None:
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.EXTENSION_MISSING,
            "public.vector type is unavailable",
        )
    operator = connection.execute(
        """
        SELECT 1 FROM pg_catalog.pg_operator o
        JOIN pg_catalog.pg_namespace n ON n.oid = o.oprnamespace
        WHERE n.nspname = 'public' AND o.oprname = '<=>'
        LIMIT 1
        """
    ).fetchone()
    if operator is None:
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.CATALOG_DRIFT,
            "public.vector cosine-distance operator is unavailable",
        )


def _verify_product_relations(connection: Connection) -> None:
    observed = tuple(
        _cell(row, 0, "relname")
        for row in connection.execute(
            """
            SELECT c.relname FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'pulsara_v3' AND c.relkind IN ('r','p')
            ORDER BY c.relname
            """
        ).fetchall()
    )
    if observed != tuple(sorted(CONVERSATION_KERNEL_RELATIONS)):
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.CATALOG_DRIFT,
            "pulsara_v3 product relation catalog is not exact",
        )


def _verify_runtime_grants(connection: Connection, runtime_role: str) -> None:
    from pulsara_agent.storage.migrations.manifest import (
        CONVERSATION_KERNEL_RUNTIME_PRIVILEGES,
    )

    schema_expectations = (
        ("pulsara_v3", "USAGE", True),
        ("pulsara_v3", "CREATE", False),
        ("public", "CREATE", False),
    )
    for schema_name, privilege, expected in schema_expectations:
        row = connection.execute(
            "SELECT pg_catalog.has_schema_privilege(%s, %s, %s) AS allowed",
            (runtime_role, schema_name, privilege),
        ).fetchone()
        if bool(_cell(row, 0, "allowed")) is not expected:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.PRIVILEGE_MISSING,
                "clean runtime schema grants are not exact",
            )

    for relation, required in CONVERSATION_KERNEL_RUNTIME_PRIVILEGES.items():
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            allowed_row = connection.execute(
                "SELECT pg_catalog.has_table_privilege(%s, %s, %s) AS allowed",
                (runtime_role, f"pulsara_v3.{relation}", privilege),
            ).fetchone()
            allowed = _cell(allowed_row, 0, "allowed")
            if bool(allowed) != (privilege in required):
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.PRIVILEGE_MISSING,
                    "clean runtime relation grants are not exact",
                )
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        allowed_row = connection.execute(
            "SELECT pg_catalog.has_table_privilege(%s, "
            "'public.pulsara_schema_migrations', %s) AS allowed",
            (runtime_role, privilege),
        ).fetchone()
        allowed = _cell(allowed_row, 0, "allowed")
        if bool(allowed) != (privilege == "SELECT"):
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.PRIVILEGE_MISSING,
                "clean runtime migration-ledger grants are not exact",
            )

    function_identity = "pulsara_v3.enforce_conversation_kernel_invariants()"
    function_execute = connection.execute(
        "SELECT pg_catalog.has_function_privilege(%s, %s, 'EXECUTE') AS allowed",
        (runtime_role, function_identity),
    ).fetchone()
    public_execute = connection.execute(
        """
        SELECT COALESCE(bool_or(a.grantee = 0 AND a.privilege_type = 'EXECUTE'), false)
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        LEFT JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))
        ) a ON true
        WHERE n.nspname = 'pulsara_v3'
          AND p.proname = 'enforce_conversation_kernel_invariants'
          AND pg_catalog.pg_get_function_identity_arguments(p.oid) = ''
        """
    ).fetchone()
    if not bool(_cell(function_execute, 0, "allowed")) or bool(
        _cell(public_execute, 0, "allowed")
    ):
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.PRIVILEGE_MISSING,
            "clean runtime invariant-function grants are not exact",
        )


def _read_identity_from_connection(connection: Connection) -> PostgresDatabaseIdentity:
    row = connection.execute(
        """
        SELECT current_database() AS database_name,
          (SELECT oid FROM pg_catalog.pg_database
            WHERE datname = current_database()) AS database_oid,
          current_user AS runtime_role,
          current_setting('search_path') AS search_path,
          current_setting('server_version_num')::int AS server_version_num
        """
    ).fetchone()
    path = tuple(
        part.strip().strip('"')
        for part in str(_cell(row, 3, "search_path")).split(",")
    )
    if path != ("public",):
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.SEARCH_PATH_MISMATCH,
            "PostgreSQL search_path must be exactly public",
        )
    return PostgresDatabaseIdentity(
        str(_cell(row, 0, "database_name")),
        int(_cell(row, 1, "database_oid")),
        str(_cell(row, 2, "runtime_role")),
        path,
        int(_cell(row, 4, "server_version_num")),
    )


def _cell(row: object, index: int, key: str) -> object:
    if isinstance(row, Mapping):
        return row[key]
    return row[index]  # type: ignore[index]


def _require_same_database(a: PostgresDatabaseIdentity, b: PostgresDatabaseIdentity) -> None:
    if (a.database_name, a.database_oid, a.server_version_num) != (
        b.database_name,
        b.database_oid,
        b.server_version_num,
    ):
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.DATABASE_IDENTITY_MISMATCH,
            "admin and runtime connections target different databases",
        )


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split(".")[:3])
    except ValueError as exc:
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.CATALOG_DRIFT,
            "vector extension version is invalid",
        ) from exc


def _reset_required(detail: str) -> PostgresSchemaError:
    return PostgresSchemaError(
        PostgresSchemaFailureCode.MIGRATION_UNIVERSE_RESET_REQUIRED,
        detail,
        retryable=False,
    )


def _report(*, identity, applied, grants, previous, confirmation, registry):
    payload = {
        "database_name": identity.database_name,
        "runtime_role": identity.runtime_role,
        "previous_head_version": previous,
        "migration_head_version": 0,
        "applied_versions": applied,
        "added_grant_fingerprints": grants,
        "registry_prefix_fingerprint": registry.registry_fingerprint,
        "universe_id": UNIVERSE_ID,
        "universe_generation": UNIVERSE_GENERATION,
        "universe_fingerprint": registry.universe_fingerprint,
        "baseline_commit_confirmation": confirmation,
    }
    from pulsara_agent.storage.migrations.contracts import postgres_schema_fingerprint

    return PostgresMigrationReport(
        status="current",
        **payload,
        report_fingerprint=postgres_schema_fingerprint(
            "pulsara:clean-migration-report:v1", payload
        ),
    )


__all__ = [
    "BaselineCommitConfirmation",
    "PostgresDatabaseIdentity",
    "PostgresMigrationReport",
    "PostgresMigrationRunner",
    "_read_identity_from_connection",
    "classify_clean_baseline_confirmation",
    "read_migration_ledger",
]
