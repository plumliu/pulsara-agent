"""Privileged, forward-only PostgreSQL schema migration runner."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from hashlib import sha256
from time import monotonic
from psycopg import Connection, sql
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb

from pulsara_agent import __version__
from pulsara_agent.storage.migrations.contracts import (
    PostgresMigrationLedgerRowFact,
    canonical_utc,
    postgres_schema_fingerprint,
)
from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)
from pulsara_agent.storage.migrations.grants import (
    PostgresFunctionGrantTargetFact,
    PostgresRelationGrantTargetFact,
    PostgresRuntimeGrantExecutor,
    PostgresRuntimeGrantRequirementFact,
    PostgresSchemaGrantTargetFact,
    PostgresTypeGrantTargetFact,
    build_postgres_runtime_grant_policy,
)
from pulsara_agent.storage.migrations.manifest import (
    PULSARA_RESERVED_RELATION_NAMES,
    build_postgres_schema_manifest,
)
from pulsara_agent.storage.migrations.registry import (
    POSTGRES_MIGRATION_REGISTRY,
    PostgresMigrationDefinition,
    PostgresMigrationRegistry,
)
from pulsara_agent.storage.postgres_endpoint import (
    PostgresCanonicalEndpointFact,
    ResolvedPostgresConnectionFactory,
)
from pulsara_agent.projection_jobs.contracts import (
    PostgresMigrationProgressOutcomeFact,
)
from pulsara_agent.projection_jobs.migration_state import (
    migration_progress_outcome,
    next_projection_migration_requirement,
)
from pulsara_agent.ports.projection_jobs import (
    ProjectionMigrationPreparationPort,
    build_projection_migration_readiness_view,
    build_projection_migration_transaction_identity,
)
from pulsara_agent.storage.projection_migration_transaction import (
    PostgresProjectionMigrationTransactionCapability,
)
from pulsara_agent.storage.runtime_write_admission import read_runtime_write_epoch


_LOCK_NAMESPACE = int.from_bytes(
    sha256(b"pulsara:postgres-schema-migration:v1").digest()[:4],
    byteorder="big",
    signed=True,
)

_PROJECTION_STAGED_MIGRATION_VERSIONS = frozenset({6, 7, 8, 9})
_MCP_V2_SCHEMA_SUBCUT_VERSION = 10
_MCP_V2_PROTECTED_RELATION_RESOURCE = "0010_runtime_write_protected_relations_v1.json"
_TERMINAL_PRESENTATION_SCHEMA_SUBCUT_VERSION = 11
_TERMINAL_PRESENTATION_PROTECTED_RELATION_RESOURCE = (
    "0011_runtime_write_protected_relations_v1.json"
)


class PostgresCommitConfirmation(StrEnum):
    FULL = "full"
    NONE = "none"
    CONFLICT = "conflict"
    UNRESOLVED = "unresolved"


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
    runtime_role_can_create_in_public_schema: bool
    warnings: tuple[str, ...]
    progress_outcome: PostgresMigrationProgressOutcomeFact
    report_fingerprint: str

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["progress_outcome"] = self.progress_outcome.model_dump(mode="json")
        return payload


class PostgresAdminConnectionFactory:
    """Dedicated privileged connection owner for schema migration."""

    def __init__(self, dsn: str) -> None:
        self._resolved = ResolvedPostgresConnectionFactory(
            dsn,
            application_name="pulsara-schema-admin",
        )

    @property
    def endpoint(self) -> PostgresCanonicalEndpointFact:
        return self._resolved.endpoint

    def connect(self, *, deadline_monotonic: float) -> Connection:
        return self._resolved.connect(
            deadline_monotonic=deadline_monotonic,
            autocommit=True,
        )


class _PostgresMigrationRuntimeConnectionFactory:
    def __init__(self, dsn: str) -> None:
        self._resolved = ResolvedPostgresConnectionFactory(
            dsn,
            application_name="pulsara-schema-runtime-probe",
        )

    @property
    def endpoint(self) -> PostgresCanonicalEndpointFact:
        return self._resolved.endpoint

    def read_identity(self, *, deadline_monotonic: float) -> PostgresDatabaseIdentity:
        with self._resolved.connect(
            deadline_monotonic=deadline_monotonic,
            autocommit=True,
        ) as connection:
            return _read_identity_from_connection(connection)


class PostgresMigrationRunner:
    def __init__(
        self,
        *,
        admin_dsn: str,
        runtime_dsn: str,
        registry: PostgresMigrationRegistry = POSTGRES_MIGRATION_REGISTRY,
        application_version: str = __version__,
        grant_executor: PostgresRuntimeGrantExecutor | None = None,
        projection_preparation_port: ProjectionMigrationPreparationPort | None = None,
    ) -> None:
        if not admin_dsn.strip():
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.ADMIN_DSN_REQUIRED,
                "PULSARA_POSTGRES_ADMIN_DSN is required for db migrate",
            )
        if not runtime_dsn.strip():
            raise ValueError("runtime_dsn must be non-empty")
        self._admin_factory = PostgresAdminConnectionFactory(admin_dsn)
        self._runtime_factory = _PostgresMigrationRuntimeConnectionFactory(runtime_dsn)
        if (
            self._admin_factory.endpoint.endpoint_fingerprint
            != self._runtime_factory.endpoint.endpoint_fingerprint
        ):
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.DATABASE_IDENTITY_MISMATCH,
                "admin and runtime DSNs must resolve to the same host/port/database target",
            )
        self._registry = registry
        self._application_version = application_version
        self._grant_executor = grant_executor or PostgresRuntimeGrantExecutor()
        self._projection_preparation_port = projection_preparation_port

    def migrate(self, *, deadline_monotonic: float) -> PostgresMigrationReport:
        try:
            self._registry.verify_resources()
        except (OSError, UnicodeError, ValueError) as exc:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.RESOURCE_CHECKSUM_MISMATCH,
                "packaged PostgreSQL migration resource verification failed",
            ) from exc
        runtime_identity = self._runtime_factory.read_identity(
            deadline_monotonic=deadline_monotonic
        )
        admin = self._admin_factory.connect(deadline_monotonic=deadline_monotonic)
        applied_versions: list[int] = []
        added_grants: tuple[str, ...] = ()
        previous_head: int | None = None
        preparation_requirement = None
        try:
            admin_identity = _read_identity_from_connection(admin)
            _validate_transaction_domain(admin_identity, runtime_identity)
            _acquire_advisory_lock(
                admin,
                database_oid=admin_identity.database_oid,
                shared=False,
                deadline_monotonic=deadline_monotonic,
            )
            rows = read_migration_ledger(admin)
            if rows is None:
                self._require_pulsara_empty(admin)
                rows_tuple: tuple[PostgresMigrationLedgerRowFact, ...] = ()
            else:
                rows_tuple = rows
                self._validate_applied_history(rows_tuple)
                previous_head = rows_tuple[-1].version if rows_tuple else None
                if previous_head is not None:
                    self._validate_postcondition(admin, previous_head)
            start_version = len(rows_tuple)
            if start_version > len(self._registry.definitions):
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.VERSION_AHEAD,
                    "database migration head is newer than this Pulsara binary",
                )
            next_version = start_version
            while next_version < len(self._registry.definitions):
                current_head_version = next_version - 1
                current_prefix = (
                    self._registry.definition(
                        current_head_version
                    ).registry_prefix_fingerprint
                    if current_head_version >= 0
                    else ""
                )
                if current_head_version in {5, 6, 7, 8}:
                    port = self._require_projection_preparation_port()
                    epoch = read_runtime_write_epoch(admin, privileged=True)
                    identity = build_projection_migration_transaction_identity(
                        database_target_fingerprint=(
                            self._admin_factory.endpoint.endpoint_fingerprint
                        ),
                        database_oid=admin_identity.database_oid,
                        backend_pid=admin.info.backend_pid,
                        current_head_version=current_head_version,
                        current_registry_prefix_fingerprint=current_prefix,
                        maintenance_operation_id=(
                            epoch.maintenance_operation_id or "readiness"
                        ),
                        maintenance_epoch_fingerprint=epoch.epoch_fingerprint,
                        transaction_generation=next_version + 1,
                    )
                    capability = PostgresProjectionMigrationTransactionCapability(
                        connection=admin,
                        identity=identity,
                        authority=port.port_authority,
                    )
                    try:
                        readiness = port.readiness(
                            transaction=capability,
                            current_head_version=current_head_version,
                            database_target_fingerprint=(
                                self._admin_factory.endpoint.endpoint_fingerprint
                            ),
                        )
                    finally:
                        capability.release()
                else:
                    readiness = build_projection_migration_readiness_view(
                        legacy_surface_binding_plan_ready=False,
                        timeline_coverage_ready=False,
                        evidence_coverage_ready=False,
                    )
                preparation_requirement = next_projection_migration_requirement(
                    current_head_version=current_head_version,
                    target_head_version=self._registry.latest_version,
                    database_target_fingerprint=(
                        self._admin_factory.endpoint.endpoint_fingerprint
                    ),
                    current_registry_prefix_fingerprint=current_prefix,
                    legacy_surface_binding_plan_ready=(
                        readiness.legacy_surface_binding_plan_ready
                    ),
                    timeline_coverage_ready=readiness.timeline_coverage_ready,
                    evidence_coverage_ready=readiness.evidence_coverage_ready,
                )
                if preparation_requirement is not None:
                    break
                definition = self._registry.definitions[next_version]
                if definition.version == 9:
                    self._require_empty_world_for_compaction_memory_extraction(admin)
                    self._enter_compaction_memory_extraction_maintenance(
                        admin,
                        definition=definition,
                        deadline_monotonic=deadline_monotonic,
                    )
                elif definition.version == _MCP_V2_SCHEMA_SUBCUT_VERSION:
                    self._require_empty_world_for_mcp_v2(admin)
                    self._enter_mcp_v2_maintenance(
                        admin,
                        definition=definition,
                        deadline_monotonic=deadline_monotonic,
                    )
                elif definition.version == _TERMINAL_PRESENTATION_SCHEMA_SUBCUT_VERSION:
                    self._enter_terminal_presentation_maintenance(
                        admin,
                        definition=definition,
                        deadline_monotonic=deadline_monotonic,
                    )
                admin = self._apply_one(
                    admin,
                    runtime_identity=runtime_identity,
                    runtime_role=runtime_identity.runtime_role,
                    definition=definition,
                    deadline_monotonic=deadline_monotonic,
                )
                applied_versions.append(definition.version)
                refreshed_rows = read_migration_ledger(admin)
                if refreshed_rows is None:
                    raise PostgresSchemaError(
                        PostgresSchemaFailureCode.MIGRATION_CONFIRMATION_CONFLICT,
                        "migration ledger disappeared after a confirmed commit",
                    )
                self._validate_applied_history(refreshed_rows)
                next_version = len(refreshed_rows)
            final_rows = read_migration_ledger(admin)
            if final_rows is None:
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.MIGRATION_FAILED,
                    "migration ledger disappeared before final verification",
                )
            final_head = final_rows[-1].version
            self._validate_applied_history(
                final_rows,
                require_latest=preparation_requirement is None,
            )
            self._validate_postcondition(admin, final_head)
            if preparation_requirement is None:
                admin, added_grants = self._reconcile_privileges(
                    admin,
                    runtime_identity=runtime_identity,
                    runtime_role=runtime_identity.runtime_role,
                    deadline_monotonic=deadline_monotonic,
                )
                from pulsara_agent.storage.migrations.catalog import (
                    PostgresCatalogCanonicalizer,
                )
                from pulsara_agent.storage.migrations.verifier import (
                    _load_expected_catalog,
                )

                expected_catalog = _load_expected_catalog(through_version=final_head)
                observed_catalog = PostgresCatalogCanonicalizer().read_deep(
                    admin,
                    relation_names=tuple(
                        str(item["relation_name"])
                        for item in expected_catalog["relation_execution_shapes"]
                    ),
                )
                if observed_catalog.deep_catalog_fingerprint != str(
                    expected_catalog["deep_catalog_fingerprint"]
                ):
                    raise PostgresSchemaError(
                        PostgresSchemaFailureCode.CATALOG_DRIFT,
                        "final migrated catalog differs from the packaged deep manifest",
                    )
            runtime_can_create_in_public = _runtime_role_can_create_in_public_schema(
                admin, runtime_identity.runtime_role
            )
            warnings = (
                ("runtime_role_can_create_in_public_schema",)
                if runtime_can_create_in_public
                else ()
            )
            progress = migration_progress_outcome(
                initial_head_version=previous_head,
                resulting_head_version=final_head,
                target_head_version=self._registry.latest_version,
                applied_versions=tuple(applied_versions),
                requirement=preparation_requirement,
                resulting_registry_prefix_fingerprint=(
                    final_rows[-1].registry_prefix_fingerprint
                ),
            )
            payload = {
                "status": (
                    "preparation_required"
                    if preparation_requirement is not None
                    else ("migrated" if applied_versions else "up_to_date")
                ),
                "database_name": runtime_identity.database_name,
                "runtime_role": runtime_identity.runtime_role,
                "previous_head_version": previous_head,
                "migration_head_version": final_head,
                "applied_versions": tuple(applied_versions),
                "added_grant_fingerprints": added_grants,
                "registry_prefix_fingerprint": (
                    final_rows[-1].registry_prefix_fingerprint
                ),
                "runtime_role_can_create_in_public_schema": (
                    runtime_can_create_in_public
                ),
                "warnings": warnings,
                "progress_outcome": progress,
            }
            return PostgresMigrationReport(
                **payload,
                report_fingerprint=postgres_schema_fingerprint(
                    "pulsara:postgres-migration-report:v1",
                    {
                        **{
                            key: value
                            for key, value in payload.items()
                            if key != "progress_outcome"
                        },
                        "progress_outcome_fingerprint": (progress.outcome_fingerprint),
                    },
                ),
            )
        finally:
            admin.close()

    def _enter_compaction_memory_extraction_maintenance(
        self,
        connection: Connection,
        *,
        definition: PostgresMigrationDefinition,
        deadline_monotonic: float,
    ) -> None:
        from pulsara_agent.projection_jobs.contracts import RuntimeWriteAdmissionMode
        from pulsara_agent.storage.runtime_write_admission import (
            enter_runtime_write_maintenance,
        )

        epoch = read_runtime_write_epoch(connection, privileged=True)
        if (
            epoch.mode is RuntimeWriteAdmissionMode.MAINTENANCE
            and epoch.target_migration_version == definition.version
        ):
            return
        if epoch.mode is not RuntimeWriteAdmissionMode.NORMAL:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.MIGRATION_FAILED,
                "migration 0009 cannot replace a different maintenance operation",
            )
        operation_id = "projection-maintenance:" + postgres_schema_fingerprint(
            "compaction-memory-extraction-activation-operation:v1",
            {
                "database_target_fingerprint": (
                    self._admin_factory.endpoint.endpoint_fingerprint
                ),
                "normal_epoch_fingerprint": epoch.epoch_fingerprint,
                "target_migration_contract_fingerprint": (
                    definition.migration_contract_fingerprint
                ),
            },
        )
        with connection.transaction():
            _apply_local_deadline(connection, deadline_monotonic)
            enter_runtime_write_maintenance(
                connection,
                current_epoch=epoch,
                maintenance_operation_id=operation_id,
                target_migration_version=definition.version,
            )

    @staticmethod
    def _require_empty_world_for_compaction_memory_extraction(
        connection: Connection,
    ) -> None:
        with connection.cursor(row_factory=tuple_row) as cursor:
            row = cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM public.sessions LIMIT 1
                ) AS world_is_nonempty
                """
            ).fetchone()
        if row is not None and bool(row[0]):
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.RESET_REQUIRED_FOR_COMPACTION_MEMORY_EXTRACTION_V1,
                "migration 0009 changes compaction-memory evidence authority; "
                "reset the PostgreSQL and Oxigraph runtime world before migration",
            )

    def _enter_mcp_v2_maintenance(
        self,
        connection: Connection,
        *,
        definition: PostgresMigrationDefinition,
        deadline_monotonic: float,
    ) -> None:
        from pulsara_agent.projection_jobs.contracts import RuntimeWriteAdmissionMode
        from pulsara_agent.storage.runtime_write_admission import (
            enter_runtime_write_maintenance,
        )

        epoch = read_runtime_write_epoch(connection, privileged=True)
        if (
            epoch.mode is RuntimeWriteAdmissionMode.MAINTENANCE
            and epoch.target_migration_version == definition.version
        ):
            return
        if epoch.mode is not RuntimeWriteAdmissionMode.NORMAL:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.MIGRATION_FAILED,
                "migration 0010 cannot replace a different maintenance operation",
            )
        operation_id = "mcp-v2-maintenance:" + postgres_schema_fingerprint(
            "mcp-v2-schema-subcut-operation:v1",
            {
                "database_target_fingerprint": (
                    self._admin_factory.endpoint.endpoint_fingerprint
                ),
                "normal_epoch_fingerprint": epoch.epoch_fingerprint,
                "target_migration_contract_fingerprint": (
                    definition.migration_contract_fingerprint
                ),
            },
        )
        with connection.transaction():
            _apply_local_deadline(connection, deadline_monotonic)
            enter_runtime_write_maintenance(
                connection,
                current_epoch=epoch,
                maintenance_operation_id=operation_id,
                target_migration_version=definition.version,
            )

    @staticmethod
    def _require_empty_world_for_mcp_v2(connection: Connection) -> None:
        with connection.cursor(row_factory=tuple_row) as cursor:
            row = cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM public.sessions LIMIT 1
                ) AS world_is_nonempty
                """
            ).fetchone()
        if row is not None and bool(row[0]):
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.RESET_REQUIRED_FOR_MCP_V2,
                "migration 0010 changes MCP suspension and materialization "
                "authority; reset the PostgreSQL and Oxigraph runtime world "
                "before migration",
            )

    def _require_projection_preparation_port(
        self,
    ) -> ProjectionMigrationPreparationPort:
        port = self._projection_preparation_port
        if port is None:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.PROJECTION_PREPARATION_PORT_REQUIRED,
                "projection migration preparation port is required for migrations 0006-0009",
            )
        return port

    def _enter_terminal_presentation_maintenance(
        self,
        connection: Connection,
        *,
        definition: PostgresMigrationDefinition,
        deadline_monotonic: float,
    ) -> None:
        from pulsara_agent.projection_jobs.contracts import RuntimeWriteAdmissionMode
        from pulsara_agent.storage.runtime_write_admission import (
            enter_runtime_write_maintenance,
        )

        epoch = read_runtime_write_epoch(connection, privileged=True)
        if (
            epoch.mode is RuntimeWriteAdmissionMode.MAINTENANCE
            and epoch.target_migration_version == definition.version
        ):
            return
        if epoch.mode is not RuntimeWriteAdmissionMode.NORMAL:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.MIGRATION_FAILED,
                "migration 0011 cannot replace a different maintenance operation",
            )
        operation_id = (
            "terminal-presentation-maintenance:"
            + postgres_schema_fingerprint(
                "terminal-presentation-schema-subcut-operation:v1",
                {
                    "database_target_fingerprint": (
                        self._admin_factory.endpoint.endpoint_fingerprint
                    ),
                    "normal_epoch_fingerprint": epoch.epoch_fingerprint,
                    "target_migration_contract_fingerprint": (
                        definition.migration_contract_fingerprint
                    ),
                },
            )
        )
        with connection.transaction():
            _apply_local_deadline(connection, deadline_monotonic)
            enter_runtime_write_maintenance(
                connection,
                current_epoch=epoch,
                maintenance_operation_id=operation_id,
                target_migration_version=definition.version,
            )

    def _require_pulsara_empty(self, connection: Connection) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT n.nspname, c.relname
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = ANY(%s)
                ORDER BY c.relname
                """,
                (sorted(PULSARA_RESERVED_RELATION_NAMES),),
            )
            relations = tuple(cursor.fetchall())
            cursor.execute(
                """
                SELECT 1
                FROM pg_catalog.pg_proc p
                JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public'
                  AND p.proname = 'pulsara_jsonb_text_array'
                """
            )
            function_exists = cursor.fetchone() is not None
        if relations or function_exists:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.UNMANAGED_DATABASE,
                "database contains Pulsara-owned objects without a migration ledger; reset is required",
            )

    def _apply_one(
        self,
        connection: Connection,
        *,
        runtime_identity: PostgresDatabaseIdentity,
        runtime_role: str,
        definition: PostgresMigrationDefinition,
        deadline_monotonic: float,
    ) -> Connection:
        while True:
            _require_remaining(deadline_monotonic)
            body_completed = False
            try:
                with connection.transaction():
                    _apply_local_deadline(connection, deadline_monotonic)
                    maintenance_epoch = None
                    if definition.version >= 6:
                        from pulsara_agent.storage.runtime_write_admission import (
                            acquire_maintenance_runtime_write_guard,
                        )

                        maintenance_epoch = read_runtime_write_epoch(
                            connection,
                            privileged=True,
                        )
                        if (
                            maintenance_epoch.mode.value != "maintenance"
                            or maintenance_epoch.target_migration_version
                            != definition.version
                        ):
                            raise PostgresSchemaError(
                                PostgresSchemaFailureCode.MIGRATION_FAILED,
                                f"migration {definition.version} requires its "
                                "prepared maintenance epoch",
                            )
                        acquire_maintenance_runtime_write_guard(
                            connection,
                            expected_epoch=maintenance_epoch,
                            transaction_owner_id=(
                                "schema-migration:"
                                + str(definition.version)
                                + ":"
                                + str(maintenance_epoch.maintenance_operation_id)
                            ),
                        )
                        if definition.version in _PROJECTION_STAGED_MIGRATION_VERSIONS:
                            port = self._require_projection_preparation_port()
                            previous = self._registry.definition(definition.version - 1)
                            identity = build_projection_migration_transaction_identity(
                                database_target_fingerprint=(
                                    self._admin_factory.endpoint.endpoint_fingerprint
                                ),
                                database_oid=runtime_identity.database_oid,
                                backend_pid=connection.info.backend_pid,
                                current_head_version=definition.version - 1,
                                current_registry_prefix_fingerprint=(
                                    previous.registry_prefix_fingerprint
                                ),
                                maintenance_operation_id=str(
                                    maintenance_epoch.maintenance_operation_id
                                ),
                                maintenance_epoch_fingerprint=(
                                    maintenance_epoch.epoch_fingerprint
                                ),
                                transaction_generation=definition.version + 1,
                            )
                            capability = (
                                PostgresProjectionMigrationTransactionCapability(
                                    connection=connection,
                                    identity=identity,
                                    authority=port.port_authority,
                                )
                            )
                            try:
                                port.apply_transform(
                                    transaction=capability,
                                    version=definition.version,
                                    maintenance_epoch=maintenance_epoch,
                                    resulting_registry_prefix_fingerprint=(
                                        definition.registry_prefix_fingerprint
                                    ),
                                )
                            finally:
                                capability.release()
                    connection.execute(definition.resource_text(), prepare=False)
                    if (
                        definition.version
                        == _TERMINAL_PRESENTATION_SCHEMA_SUBCUT_VERSION
                    ):
                        from pulsara_agent.storage.prompt_queue_bootstrap import (
                            install_prompt_queue_genesis,
                        )

                        session_ids = tuple(
                            str(row[0])
                            for row in connection.execute(
                                "SELECT id FROM public.sessions ORDER BY id"
                            ).fetchall()
                        )
                        for runtime_session_id in session_ids:
                            install_prompt_queue_genesis(
                                connection,
                                runtime_session_id=runtime_session_id,
                                require_empty_queue_chain=True,
                            )
                    if definition.version == 5:
                        from pulsara_agent.storage.runtime_write_admission import (
                            install_runtime_write_admission_v5,
                        )

                        install_runtime_write_admission_v5(
                            connection,
                            database_target_fingerprint=(
                                self._runtime_factory.endpoint.endpoint_fingerprint
                            ),
                            runtime_role=runtime_role,
                            resulting_registry_prefix_fingerprint=(
                                definition.registry_prefix_fingerprint
                            ),
                        )
                    elif definition.version >= 6:
                        assert maintenance_epoch is not None
                        from pulsara_agent.storage.runtime_write_admission import (
                            install_runtime_write_normal_epoch,
                            replace_runtime_write_protected_relation_registry,
                        )

                        if definition.version in _PROJECTION_STAGED_MIGRATION_VERSIONS:
                            resource_name = self._require_projection_preparation_port().protected_relation_resource_for_version(
                                definition.version
                            )
                        elif definition.version == _MCP_V2_SCHEMA_SUBCUT_VERSION:
                            resource_name = _MCP_V2_PROTECTED_RELATION_RESOURCE
                        elif (
                            definition.version
                            == _TERMINAL_PRESENTATION_SCHEMA_SUBCUT_VERSION
                        ):
                            resource_name = (
                                _TERMINAL_PRESENTATION_PROTECTED_RELATION_RESOURCE
                            )
                        else:
                            raise PostgresSchemaError(
                                PostgresSchemaFailureCode.MIGRATION_FAILED,
                                "migration has no closed maintenance family",
                            )
                        replace_runtime_write_protected_relation_registry(
                            connection,
                            resource_name=resource_name,
                        )
                        install_runtime_write_normal_epoch(
                            connection,
                            maintenance_epoch=maintenance_epoch,
                            resulting_registry_prefix_fingerprint=(
                                definition.registry_prefix_fingerprint
                            ),
                            protected_relation_resource_name=resource_name,
                        )
                    policy = build_postgres_runtime_grant_policy(definition.version)
                    for requirement in policy.requirements:
                        self._grant_executor.apply_requirement(
                            connection,
                            runtime_role=runtime_role,
                            requirement=requirement,
                        )
                    self._validate_postcondition(connection, definition.version)
                    connection.execute(
                        """
                        INSERT INTO public.pulsara_schema_migrations (
                            version,
                            name,
                            checksum,
                            migration_contract_fingerprint,
                            registry_prefix_fingerprint,
                            application_version
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            definition.version,
                            definition.name,
                            definition.expected_sha256,
                            definition.migration_contract_fingerprint,
                            definition.registry_prefix_fingerprint,
                            self._application_version,
                        ),
                    )
                    body_completed = True
                return connection
            except PostgresSchemaError:
                raise
            except Exception as exc:
                if not body_completed:
                    raise PostgresSchemaError(
                        PostgresSchemaFailureCode.MIGRATION_FAILED,
                        f"migration {definition.version} failed: {type(exc).__name__}",
                    ) from exc
                connection.close()
                confirmation, confirmed_connection = self._confirm_migration_commit(
                    runtime_identity=runtime_identity,
                    definition=definition,
                    deadline_monotonic=deadline_monotonic,
                )
                if confirmation is PostgresCommitConfirmation.FULL:
                    assert confirmed_connection is not None
                    return confirmed_connection
                if confirmation is PostgresCommitConfirmation.NONE:
                    assert confirmed_connection is not None
                    connection = confirmed_connection
                    continue
                if confirmed_connection is not None:
                    confirmed_connection.close()
                if confirmation is PostgresCommitConfirmation.UNRESOLVED:
                    raise PostgresSchemaError(
                        PostgresSchemaFailureCode.MIGRATION_CONFIRMATION_UNRESOLVED,
                        f"migration {definition.version} commit outcome is unresolved",
                        retryable=True,
                    ) from exc
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.MIGRATION_CONFIRMATION_CONFLICT,
                    f"migration {definition.version} commit conflicts with ledger/catalog authority",
                ) from exc

    def _confirm_migration_commit(
        self,
        *,
        runtime_identity: PostgresDatabaseIdentity,
        definition: PostgresMigrationDefinition,
        deadline_monotonic: float,
    ) -> tuple[PostgresCommitConfirmation, Connection | None]:
        connection: Connection | None = None
        try:
            connection = self._admin_factory.connect(
                deadline_monotonic=deadline_monotonic
            )
            admin_identity = _read_identity_from_connection(connection)
            _validate_transaction_domain(admin_identity, runtime_identity)
            _acquire_advisory_lock(
                connection,
                database_oid=admin_identity.database_oid,
                shared=False,
                deadline_monotonic=deadline_monotonic,
            )
        except PostgresSchemaError as exc:
            if connection is not None:
                connection.close()
            if exc.code not in {
                PostgresSchemaFailureCode.CONNECTION_FAILED,
                PostgresSchemaFailureCode.DEADLINE_EXCEEDED,
            }:
                return PostgresCommitConfirmation.CONFLICT, None
            return PostgresCommitConfirmation.UNRESOLVED, None
        except Exception:
            if connection is not None:
                connection.close()
            return PostgresCommitConfirmation.UNRESOLVED, None

        try:
            rows = read_migration_ledger(connection)
            if _candidate_row_is_full(rows, definition):
                assert rows is not None
                self._validate_applied_history(rows)
                self._validate_postcondition(connection, definition.version)
                return PostgresCommitConfirmation.FULL, connection

            previous_rows = () if rows is None else rows
            if (
                len(previous_rows) == definition.version
                and _rows_match_registry_prefix(
                    previous_rows,
                    self._registry,
                )
                and _catalog_matches_manifest(
                    connection,
                    through_version=definition.version - 1,
                )
            ):
                return PostgresCommitConfirmation.NONE, connection
            return PostgresCommitConfirmation.CONFLICT, connection
        except PostgresSchemaError:
            return PostgresCommitConfirmation.CONFLICT, connection
        except Exception:
            connection.close()
            return PostgresCommitConfirmation.UNRESOLVED, None

    @staticmethod
    def _validate_postcondition(connection: Connection, through_version: int) -> None:
        manifest = build_postgres_schema_manifest(through_version)
        relation_names = tuple(
            str(item["relation_name"]) for item in manifest.owned_relations
        )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.relname
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = ANY(%s)
                ORDER BY c.relname
                """,
                (list(relation_names),),
            )
            observed = tuple(row[0] for row in cursor.fetchall())
            if observed != tuple(sorted(relation_names)):
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.CATALOG_DRIFT,
                    f"migration {through_version} relation postcondition failed",
                )
            if through_version >= 1:
                cursor.execute(
                    "SELECT extversion FROM pg_catalog.pg_extension WHERE extname = 'vector'"
                )
                row = cursor.fetchone()
                if row is None:
                    raise PostgresSchemaError(
                        PostgresSchemaFailureCode.EXTENSION_MISSING,
                        "pgvector extension is absent after migration 0001",
                    )
                if _version_tuple(str(row[0])) < (0, 5, 0):
                    raise PostgresSchemaError(
                        PostgresSchemaFailureCode.EXTENSION_TOO_OLD,
                        "pgvector extension must be >= 0.5.0",
                    )
            if through_version >= 5:
                cursor.execute(
                    "SELECT extversion FROM pg_catalog.pg_extension "
                    "WHERE extname = 'pgcrypto'"
                )
                if cursor.fetchone() is None:
                    raise PostgresSchemaError(
                        PostgresSchemaFailureCode.EXTENSION_MISSING,
                        "pgcrypto extension is absent after migration 0005",
                    )
                from pulsara_agent.storage.runtime_write_admission import (
                    build_runtime_write_protected_relation_registry,
                )

                registry = build_runtime_write_protected_relation_registry(
                    resource_name=(
                        "0005_runtime_write_protected_relations_v1.json"
                        if through_version == 5
                        else (
                            "0006_runtime_write_protected_relations_v2.json"
                            if through_version <= 8
                            else (
                                "0009_runtime_write_protected_relations_v1.json"
                                if through_version == 9
                                else (
                                    _MCP_V2_PROTECTED_RELATION_RESOURCE
                                    if through_version == 10
                                    else _TERMINAL_PRESENTATION_PROTECTED_RELATION_RESOURCE
                                )
                            )
                        )
                    )
                )
                cursor.execute(
                    """
                    SELECT schema_name, relation_name, relation_fingerprint,
                           registry_fingerprint
                    FROM public.runtime_write_protected_relations
                    ORDER BY schema_name, relation_name
                    """
                )
                observed = tuple(cursor.fetchall())
                expected = tuple(
                    (
                        item.schema_name,
                        item.relation_name,
                        item.relation_fingerprint,
                        registry.registry_fingerprint,
                    )
                    for item in registry.ordered_relations
                )
                if observed != expected:
                    raise PostgresSchemaError(
                        PostgresSchemaFailureCode.CATALOG_DRIFT,
                        "runtime write protected-relation registry drifted",
                    )

    def _validate_applied_history(
        self,
        rows: tuple[PostgresMigrationLedgerRowFact, ...],
        *,
        require_latest: bool = False,
    ) -> None:
        if not rows:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.HISTORY_CONFLICT,
                "migration ledger exists but has no genesis row",
            )
        versions = tuple(row.version for row in rows)
        if versions != tuple(range(len(rows))):
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.HISTORY_CONFLICT,
                "migration ledger versions are not contiguous from zero",
            )
        if len(rows) > len(self._registry.definitions):
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.VERSION_AHEAD,
                "database migration head is newer than this binary",
            )
        for row, expected in zip(
            rows,
            self._registry.definitions[: len(rows)],
            strict=True,
        ):
            if (
                row.name != expected.name
                or row.resource_checksum != expected.expected_sha256
                or row.migration_contract_fingerprint
                != expected.migration_contract_fingerprint
                or row.registry_prefix_fingerprint
                != expected.registry_prefix_fingerprint
            ):
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.HISTORY_CONFLICT,
                    f"migration ledger row {row.version} does not match the packaged registry",
                )
        if require_latest and len(rows) != len(self._registry.definitions):
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.VERSION_BEHIND,
                "database migration head is behind this binary",
            )

    def _reconcile_privileges(
        self,
        connection: Connection,
        *,
        runtime_identity: PostgresDatabaseIdentity,
        runtime_role: str,
        deadline_monotonic: float,
    ) -> tuple[Connection, tuple[str, ...]]:
        policy = build_postgres_runtime_grant_policy(self._registry.latest_version)
        while True:
            epoch_role_rebind_required = _runtime_epoch_role_rebind_required(
                connection,
                runtime_role=runtime_role,
                latest_version=self._registry.latest_version,
            )
            missing = tuple(
                requirement
                for requirement in policy.requirements
                if not requirement_satisfied(connection, runtime_role, requirement)
            )
            if not missing and not epoch_role_rebind_required:
                return connection, ()
            body_completed = False
            try:
                with connection.transaction():
                    _apply_local_deadline(connection, deadline_monotonic)
                    rows = read_migration_ledger(connection)
                    if rows is None:
                        raise PostgresSchemaError(
                            PostgresSchemaFailureCode.HISTORY_CONFLICT,
                            "migration ledger disappeared during privilege reconciliation",
                        )
                    self._validate_applied_history(rows, require_latest=True)
                    _preflight_grant_authority(connection, missing)
                    for requirement in missing:
                        self._grant_executor.apply_requirement(
                            connection,
                            runtime_role=runtime_role,
                            requirement=requirement,
                        )
                    if epoch_role_rebind_required:
                        _rebind_runtime_write_epoch_role(
                            connection,
                            runtime_role=runtime_role,
                        )
                    unresolved = tuple(
                        requirement.requirement_fingerprint
                        for requirement in policy.requirements
                        if not requirement_satisfied(
                            connection, runtime_role, requirement
                        )
                    )
                    if unresolved:
                        raise PostgresSchemaError(
                            PostgresSchemaFailureCode.PRIVILEGE_RECONCILIATION_FAILED,
                            "runtime privilege reconciliation did not reach its complete post-state",
                        )
                    body_completed = True
                return connection, tuple(
                    item.requirement_fingerprint for item in missing
                )
            except PostgresSchemaError:
                raise
            except Exception as exc:
                if not body_completed:
                    raise PostgresSchemaError(
                        PostgresSchemaFailureCode.PRIVILEGE_RECONCILIATION_FAILED,
                        f"runtime privilege reconciliation failed: {type(exc).__name__}",
                    ) from exc
                connection.close()
                confirmation, confirmed_connection = self._confirm_privilege_commit(
                    runtime_identity=runtime_identity,
                    runtime_role=runtime_role,
                    pre_state_missing=missing,
                    pre_epoch_role_rebind_required=(epoch_role_rebind_required),
                    deadline_monotonic=deadline_monotonic,
                )
                if confirmation is PostgresCommitConfirmation.FULL:
                    assert confirmed_connection is not None
                    return confirmed_connection, tuple(
                        item.requirement_fingerprint for item in missing
                    )
                if confirmation is PostgresCommitConfirmation.NONE:
                    assert confirmed_connection is not None
                    connection = confirmed_connection
                    continue
                if confirmed_connection is not None:
                    confirmed_connection.close()
                if confirmation is PostgresCommitConfirmation.UNRESOLVED:
                    raise PostgresSchemaError(
                        PostgresSchemaFailureCode.PRIVILEGE_CONFIRMATION_UNRESOLVED,
                        "runtime privilege reconciliation commit outcome is unresolved",
                        retryable=True,
                    ) from exc
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.PRIVILEGE_CONFIRMATION_CONFLICT,
                    "runtime privilege reconciliation reached a partial/conflicting state",
                ) from exc

    def _confirm_privilege_commit(
        self,
        *,
        runtime_identity: PostgresDatabaseIdentity,
        runtime_role: str,
        pre_state_missing: tuple[PostgresRuntimeGrantRequirementFact, ...],
        pre_epoch_role_rebind_required: bool,
        deadline_monotonic: float,
    ) -> tuple[PostgresCommitConfirmation, Connection | None]:
        connection: Connection | None = None
        try:
            connection = self._admin_factory.connect(
                deadline_monotonic=deadline_monotonic
            )
            admin_identity = _read_identity_from_connection(connection)
            _validate_transaction_domain(admin_identity, runtime_identity)
            _acquire_advisory_lock(
                connection,
                database_oid=admin_identity.database_oid,
                shared=False,
                deadline_monotonic=deadline_monotonic,
            )
            rows = read_migration_ledger(connection)
            if rows is None:
                return PostgresCommitConfirmation.CONFLICT, connection
            self._validate_applied_history(rows, require_latest=True)
            policy = build_postgres_runtime_grant_policy(self._registry.latest_version)
            observed_missing = tuple(
                requirement
                for requirement in policy.requirements
                if not requirement_satisfied(connection, runtime_role, requirement)
            )
            observed_epoch_rebind_required = _runtime_epoch_role_rebind_required(
                connection,
                runtime_role=runtime_role,
                latest_version=self._registry.latest_version,
            )
        except PostgresSchemaError as exc:
            if exc.code in {
                PostgresSchemaFailureCode.CONNECTION_FAILED,
                PostgresSchemaFailureCode.DEADLINE_EXCEEDED,
            }:
                if connection is not None:
                    connection.close()
                return PostgresCommitConfirmation.UNRESOLVED, None
            return PostgresCommitConfirmation.CONFLICT, connection
        except Exception:
            if connection is not None:
                connection.close()
            return PostgresCommitConfirmation.UNRESOLVED, None

        if not observed_missing and not observed_epoch_rebind_required:
            return PostgresCommitConfirmation.FULL, connection
        if (
            tuple(item.requirement_fingerprint for item in observed_missing)
            == tuple(item.requirement_fingerprint for item in pre_state_missing)
            and observed_epoch_rebind_required == pre_epoch_role_rebind_required
        ):
            return PostgresCommitConfirmation.NONE, connection
        return PostgresCommitConfirmation.CONFLICT, connection


def read_migration_ledger(
    connection: Connection,
) -> tuple[PostgresMigrationLedgerRowFact, ...] | None:
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT pg_catalog.to_regclass('public.pulsara_schema_migrations') AS relation"
        )
        if cursor.fetchone()["relation"] is None:
            return None
        cursor.execute(
            """
            SELECT version,
                   name,
                   checksum,
                   migration_contract_fingerprint,
                   registry_prefix_fingerprint,
                   application_version,
                   applied_at
            FROM public.pulsara_schema_migrations
            ORDER BY version
            """
        )
        return tuple(
            PostgresMigrationLedgerRowFact(
                schema_version="postgres_migration_ledger_row.v1",
                version=int(row["version"]),
                name=str(row["name"]),
                resource_checksum=str(row["checksum"]),
                migration_contract_fingerprint=str(
                    row["migration_contract_fingerprint"]
                ),
                registry_prefix_fingerprint=str(row["registry_prefix_fingerprint"]),
                application_version=str(row["application_version"]),
                applied_at_utc=canonical_utc(row["applied_at"]),
            )
            for row in cursor.fetchall()
        )


def requirement_satisfied(
    connection: Connection,
    runtime_role: str,
    requirement: PostgresRuntimeGrantRequirementFact,
) -> bool:
    target = requirement.target
    with connection.cursor() as cursor:
        for privilege in requirement.ordered_required_privileges:
            if isinstance(target, PostgresSchemaGrantTargetFact):
                cursor.execute(
                    "SELECT pg_catalog.has_schema_privilege(%s, %s, %s)",
                    (runtime_role, target.schema_name, privilege),
                )
            elif isinstance(target, PostgresRelationGrantTargetFact):
                cursor.execute(
                    "SELECT pg_catalog.has_table_privilege(%s, %s, %s)",
                    (
                        runtime_role,
                        f"{target.schema_name}.{target.relation_name}",
                        privilege,
                    ),
                )
            elif isinstance(target, PostgresFunctionGrantTargetFact):
                arguments = ",".join(target.ordered_argument_types)
                cursor.execute(
                    "SELECT pg_catalog.has_function_privilege(%s, %s, %s)",
                    (
                        runtime_role,
                        f"{target.schema_name}.{target.function_name}({arguments})",
                        privilege,
                    ),
                )
            elif isinstance(target, PostgresTypeGrantTargetFact):
                cursor.execute(
                    "SELECT pg_catalog.has_type_privilege(%s, %s, %s)",
                    (
                        runtime_role,
                        f"{target.schema_name}.{target.type_name}",
                        privilege,
                    ),
                )
            else:  # pragma: no cover - closed union
                raise TypeError("unknown grant target")
            if cursor.fetchone()[0] is not True:
                return False
    return True


def _runtime_epoch_role_rebind_required(
    connection: Connection,
    *,
    runtime_role: str,
    latest_version: int,
) -> bool:
    if latest_version < 5:
        return False
    from pulsara_agent.storage.runtime_write_admission import (
        read_runtime_write_epoch,
    )

    epoch = read_runtime_write_epoch(connection, privileged=True)
    return epoch.authorized_runtime_role != runtime_role


def _rebind_runtime_write_epoch_role(
    connection: Connection,
    *,
    runtime_role: str,
) -> None:
    from pulsara_agent.projection_jobs.contracts import (
        RuntimeWriteAdmissionMode,
    )
    from pulsara_agent.storage.runtime_write_admission import (
        build_runtime_write_epoch,
        read_runtime_write_epoch,
    )

    current = read_runtime_write_epoch(connection, privileged=True)
    if current.mode is not RuntimeWriteAdmissionMode.NORMAL:
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.PRIVILEGE_RECONCILIATION_FAILED,
            "runtime role cannot be rebound while write admission is in maintenance",
        )
    if current.authorized_runtime_role == runtime_role:
        return
    resulting = build_runtime_write_epoch(
        database_target_fingerprint=current.database_target_fingerprint,
        epoch_number=current.epoch_number + 1,
        mode=RuntimeWriteAdmissionMode.NORMAL,
        authorized_runtime_role=runtime_role,
        active_migration_registry_prefix_fingerprint=(
            current.active_migration_registry_prefix_fingerprint
        ),
        protected_relation_registry_fingerprint=(
            current.protected_relation_registry_fingerprint
        ),
        maintenance_operation_id=None,
        target_migration_version=None,
        state_revision=current.state_revision + 1,
    )
    connection.execute(
        """
        UPDATE runtime_write_guard_secrets
        SET authorized_runtime_role = %s
        WHERE singleton
        """,
        (runtime_role,),
    )
    connection.execute(
        """
        UPDATE runtime_write_admission_epochs
        SET epoch_number = %s,
            authorized_runtime_role = %s,
            state_revision = %s,
            epoch_payload = %s,
            epoch_fingerprint = %s,
            updated_at = now()
        WHERE singleton
          AND epoch_fingerprint = %s
        """,
        (
            resulting.epoch_number,
            runtime_role,
            resulting.state_revision,
            Jsonb(resulting.model_dump(mode="json")),
            resulting.epoch_fingerprint,
            current.epoch_fingerprint,
        ),
    )
    if (
        connection.execute(
            "SELECT epoch_fingerprint FROM runtime_write_admission_epochs WHERE singleton"
        ).fetchone()[0]
        != resulting.epoch_fingerprint
    ):
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.PRIVILEGE_RECONCILIATION_FAILED,
            "runtime write admission role rebind compare-and-set failed",
        )


def _candidate_row_is_full(
    rows: tuple[PostgresMigrationLedgerRowFact, ...] | None,
    definition: PostgresMigrationDefinition,
) -> bool:
    if rows is None or len(rows) <= definition.version:
        return False
    row = rows[definition.version]
    return (
        row.version == definition.version
        and row.name == definition.name
        and row.resource_checksum == definition.expected_sha256
        and row.migration_contract_fingerprint
        == definition.migration_contract_fingerprint
        and row.registry_prefix_fingerprint == definition.registry_prefix_fingerprint
    )


def _rows_match_registry_prefix(
    rows: tuple[PostgresMigrationLedgerRowFact, ...],
    registry: PostgresMigrationRegistry,
) -> bool:
    if len(rows) > len(registry.definitions):
        return False
    for row, expected in zip(
        rows,
        registry.definitions[: len(rows)],
        strict=True,
    ):
        if (
            row.version != expected.version
            or row.name != expected.name
            or row.resource_checksum != expected.expected_sha256
            or row.migration_contract_fingerprint
            != expected.migration_contract_fingerprint
            or row.registry_prefix_fingerprint != expected.registry_prefix_fingerprint
        ):
            return False
    return tuple(row.version for row in rows) == tuple(range(len(rows)))


def _catalog_matches_manifest(
    connection: Connection,
    *,
    through_version: int,
) -> bool:
    expected_relations: tuple[str, ...]
    require_function = through_version >= 3
    require_vector = through_version >= 1
    if through_version < 0:
        expected_relations = ()
    else:
        manifest = build_postgres_schema_manifest(through_version)
        expected_relations = tuple(
            sorted(str(item["relation_name"]) for item in manifest.owned_relations)
        )
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = ANY(%s)
            ORDER BY c.relname
            """,
            (sorted(PULSARA_RESERVED_RELATION_NAMES),),
        )
        observed_relations = tuple(str(row[0]) for row in cursor.fetchall())
        if observed_relations != expected_relations:
            return False
        cursor.execute(
            """
            SELECT count(*)
            FROM pg_catalog.pg_proc p
            JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public'
              AND p.proname = 'pulsara_jsonb_text_array'
            """
        )
        function_count = int(cursor.fetchone()[0])
        if function_count != (1 if require_function else 0):
            return False
        if require_vector:
            cursor.execute(
                "SELECT extversion FROM pg_catalog.pg_extension WHERE extname = 'vector'"
            )
            row = cursor.fetchone()
            if row is None or _version_tuple(str(row[0])) < (0, 5, 0):
                return False
    return True


def _preflight_grant_authority(
    connection: Connection,
    requirements: tuple[PostgresRuntimeGrantRequirementFact, ...],
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user, rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user"
        )
        identity = cursor.fetchone()
        if identity is None:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.PRIVILEGE_RECONCILIATION_FAILED,
                "admin role identity is unavailable",
            )
        admin_role, is_superuser = str(identity[0]), bool(identity[1])
        if is_superuser:
            return
        for requirement in requirements:
            target = requirement.target
            if isinstance(target, PostgresSchemaGrantTargetFact):
                cursor.execute(
                    """
                    SELECT n.nspowner
                    FROM pg_catalog.pg_namespace n
                    WHERE n.nspname = %s
                    """,
                    (target.schema_name,),
                )
            elif isinstance(target, PostgresRelationGrantTargetFact):
                cursor.execute(
                    """
                    SELECT c.relowner
                    FROM pg_catalog.pg_class c
                    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = %s AND c.relname = %s
                    """,
                    (target.schema_name, target.relation_name),
                )
            elif isinstance(target, PostgresFunctionGrantTargetFact):
                function_identity = (
                    f"{target.schema_name}.{target.function_name}("
                    + ",".join(target.ordered_argument_types)
                    + ")"
                )
                cursor.execute(
                    """
                    SELECT p.proowner
                    FROM pg_catalog.pg_proc p
                    WHERE p.oid = pg_catalog.to_regprocedure(%s)
                    """,
                    (function_identity,),
                )
            elif isinstance(target, PostgresTypeGrantTargetFact):
                cursor.execute(
                    """
                    SELECT t.typowner
                    FROM pg_catalog.pg_type t
                    JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
                    WHERE n.nspname = %s AND t.typname = %s
                    """,
                    (target.schema_name, target.type_name),
                )
            else:  # pragma: no cover - closed union
                raise TypeError("unknown grant target")
            owner_row = cursor.fetchone()
            if owner_row is None:
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.PRIVILEGE_RECONCILIATION_FAILED,
                    "grant target disappeared during reconciliation preflight",
                )
            cursor.execute(
                "SELECT pg_catalog.pg_has_role(%s, %s, 'USAGE')",
                (admin_role, int(owner_row[0])),
            )
            if cursor.fetchone()[0] is not True:
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.PRIVILEGE_RECONCILIATION_FAILED,
                    "admin role lacks grant authority for the complete missing set",
                )


def _read_identity_from_connection(connection: Connection) -> PostgresDatabaseIdentity:
    with connection.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(
            """
            SELECT current_database(),
                   (SELECT oid::bigint FROM pg_catalog.pg_database WHERE datname = current_database()),
                   current_user,
                   current_schemas(false),
                   current_setting('server_version_num')::integer
            """
        )
        row = cursor.fetchone()
    assert row is not None
    identity = PostgresDatabaseIdentity(
        database_name=str(row[0]),
        database_oid=int(row[1]),
        runtime_role=str(row[2]),
        normalized_search_path=tuple(str(item) for item in row[3]),
        server_version_num=int(row[4]),
    )
    if identity.normalized_search_path != ("public",):
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.SEARCH_PATH_MISMATCH,
            "current_schemas(false) must equal ('public',)",
        )
    if not 150000 <= identity.server_version_num < 180000:
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.SERVER_VERSION_UNSUPPORTED,
            "PostgreSQL server version must be >= 15 and < 18",
        )
    return identity


def _validate_transaction_domain(
    admin: PostgresDatabaseIdentity, runtime: PostgresDatabaseIdentity
) -> None:
    if (
        admin.database_name != runtime.database_name
        or admin.database_oid != runtime.database_oid
    ):
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.DATABASE_IDENTITY_MISMATCH,
            "admin and runtime DSNs must address the same database OID/name",
        )


def _acquire_advisory_lock(
    connection: Connection,
    *,
    database_oid: int,
    shared: bool,
    deadline_monotonic: float,
) -> None:
    function = "pg_try_advisory_lock_shared" if shared else "pg_try_advisory_lock"
    database_key = ((database_oid + 2**31) % 2**32) - 2**31
    while True:
        _require_remaining(deadline_monotonic)
        row = connection.execute(
            sql.SQL("SELECT pg_catalog.{}(%s, %s)").format(sql.Identifier(function)),
            (_LOCK_NAMESPACE, database_key),
        ).fetchone()
        if row is not None and row[0] is True:
            return
        time.sleep(min(0.05, _require_remaining(deadline_monotonic)))


def _apply_local_deadline(connection: Connection, deadline_monotonic: float) -> None:
    milliseconds = max(1, int(_require_remaining(deadline_monotonic) * 1000))
    connection.execute(
        "SELECT pg_catalog.set_config('lock_timeout', %s, true)",
        (f"{milliseconds}ms",),
    )
    connection.execute(
        "SELECT pg_catalog.set_config('statement_timeout', %s, true)",
        (f"{milliseconds}ms",),
    )


def _require_remaining(deadline_monotonic: float) -> float:
    remaining = deadline_monotonic - monotonic()
    if remaining <= 0:
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.DEADLINE_EXCEEDED,
            "PostgreSQL schema operation deadline exceeded",
            retryable=True,
        )
    return remaining


def _runtime_role_can_create_in_public_schema(
    connection: Connection,
    role: str,
) -> bool:
    row = connection.execute(
        """
        SELECT r.rolsuper
               OR pg_catalog.has_schema_privilege(%s, 'public', 'CREATE')
        FROM pg_catalog.pg_roles r
        WHERE r.rolname = %s
        """,
        (role, role),
    ).fetchone()
    return bool(row and row[0])


def _version_tuple(value: str) -> tuple[int, ...]:
    result = []
    for part in value.split("."):
        digits = "".join(character for character in part if character.isdigit())
        if not digits:
            break
        result.append(int(digits))
    return tuple(result)


__all__ = [
    "PostgresCommitConfirmation",
    "PostgresDatabaseIdentity",
    "PostgresMigrationReport",
    "PostgresMigrationRunner",
    "read_migration_ledger",
    "requirement_satisfied",
]
