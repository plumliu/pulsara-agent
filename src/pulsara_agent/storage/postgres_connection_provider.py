"""Verifier-owned PostgreSQL connection authority without generic write epochs."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from threading import RLock
from time import monotonic
from typing import ContextManager, Iterator, Protocol
from uuid import uuid4

from psycopg import Connection, IsolationLevel

from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)
from pulsara_agent.storage.migrations.registry import POSTGRES_MIGRATION_REGISTRY
from pulsara_agent.storage.migrations.runner import (
    PostgresDatabaseIdentity,
    _read_identity_from_connection,
    _validate_clean_ledger,
    read_migration_ledger,
)
from pulsara_agent.storage.migrations.verifier import (
    PostgresDeepVerificationBundle,
    PostgresFastVerificationBundle,
    PostgresSchemaVerifier,
)
from pulsara_agent.storage.postgres_endpoint import (
    PostgresPhysicalOperationControl,
    ResolvedPostgresConnectionFactory,
)
from pulsara_agent.storage.schema_contract import VerifiedPostgresSchemaBinding


class PostgresConnectionLane(StrEnum):
    ARTIFACT = "artifact"
    HOST_CONTROL = "host_control"
    MEMORY_QUERY = "memory_query"
    MEMORY_MAINTENANCE = "memory_maintenance"
    INSPECTOR = "inspector"
    BACKGROUND_WORK = "background_work"


@dataclass(frozen=True, slots=True)
class PostgresPreflightIdentity:
    database_target_fingerprint: str
    database_identity: PostgresDatabaseIdentity


class PostgresRuntimeConnectionFactory:
    def __init__(self, dsn: str) -> None:
        self._resolved = ResolvedPostgresConnectionFactory(
            dsn, application_name="pulsara-runtime"
        )
        self.database_target_fingerprint = self._resolved.endpoint.endpoint_fingerprint

    @property
    def runtime_role(self) -> str:
        return self._resolved.expected_role

    def connect(self, *, deadline_monotonic: float, row_factory=None, autocommit=False):
        return self._resolved.connect(
            deadline_monotonic=deadline_monotonic,
            row_factory=row_factory,
            autocommit=autocommit,
        )

    def validate_effective_endpoint(self, connection: Connection) -> None:
        self._resolved.validate_effective_endpoint(connection)

    def preflight(
        self,
        *,
        deadline_monotonic: float,
        operation_control: PostgresPhysicalOperationControl | None = None,
    ) -> PostgresPreflightIdentity:
        control = operation_control or PostgresPhysicalOperationControl(
            deadline_monotonic=deadline_monotonic
        )
        owns_control = operation_control is None
        if owns_control:
            control.arm()
        try:
            with self.connect(deadline_monotonic=deadline_monotonic, autocommit=True) as connection:
                identity = _read_identity_from_connection(connection)
        finally:
            if owns_control:
                control.finish()
        return PostgresPreflightIdentity(self.database_target_fingerprint, identity)

    def verify(
        self,
        *,
        deadline_monotonic: float,
        operation_control: PostgresPhysicalOperationControl | None = None,
    ) -> PostgresFastVerificationBundle:
        del operation_control
        with self.connect(deadline_monotonic=deadline_monotonic, autocommit=True) as connection:
            return PostgresSchemaVerifier().verify_fast_connection(
                connection,
                database_target_fingerprint=self.database_target_fingerprint,
                deadline_monotonic=deadline_monotonic,
            )

    def verify_deep(
        self,
        *,
        deadline_monotonic: float,
        operation_control: PostgresPhysicalOperationControl | None = None,
    ) -> PostgresDeepVerificationBundle:
        del operation_control
        with self.connect(deadline_monotonic=deadline_monotonic, autocommit=True) as connection:
            return PostgresSchemaVerifier().verify_deep_connection(
                connection,
                database_target_fingerprint=self.database_target_fingerprint,
                deadline_monotonic=deadline_monotonic,
            )


class VerifiedPostgresConnectionProviderProtocol(Protocol):
    @property
    def schema_binding(self) -> VerifiedPostgresSchemaBinding: ...

    @property
    def verification_observed_at_utc(self) -> str: ...

    def connection(
        self,
        *,
        lane: PostgresConnectionLane,
        row_factory: object | None = None,
        deadline_monotonic: float,
        isolation_level: IsolationLevel = IsolationLevel.READ_COMMITTED,
    ) -> ContextManager[Connection]: ...


class BorrowedVerifiedPostgresConnectionProvider:
    def __init__(self, provider: "VerifiedPostgresConnectionProvider") -> None:
        self._provider = provider
        self._released = False
        self._borrower_id = f"postgres-borrower:{uuid4().hex}"
        self._lock = RLock()
        provider._retain_borrower()

    @property
    def schema_binding(self) -> VerifiedPostgresSchemaBinding:
        self._require_active()
        return self._provider.schema_binding

    @property
    def verification_observed_at_utc(self) -> str:
        self._require_active()
        return self._provider.verification_observed_at_utc

    @contextmanager
    def connection(self, **kwargs) -> Iterator[Connection]:
        self._require_active()
        with self._provider.connection(**kwargs) as connection:
            yield connection

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._provider._release_borrower()

    def _require_active(self) -> None:
        with self._lock:
            if self._released:
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.ACCESS_LEASE_RELEASED,
                    "verified PostgreSQL access lease is released",
                )


class VerifiedPostgresConnectionProvider:
    def __init__(
        self,
        *,
        factory: PostgresRuntimeConnectionFactory,
        binding: VerifiedPostgresSchemaBinding,
    ) -> None:
        self._factory = factory
        self._binding = binding
        self._verification_observed_at_utc = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        self._lock = RLock()
        self._borrowers = 0
        self._closing = False
        self._invalidated = False

    @property
    def schema_binding(self) -> VerifiedPostgresSchemaBinding:
        return self._binding

    @property
    def verification_observed_at_utc(self) -> str:
        return self._verification_observed_at_utc

    def borrow(self) -> BorrowedVerifiedPostgresConnectionProvider:
        return BorrowedVerifiedPostgresConnectionProvider(self)

    @contextmanager
    def connection(
        self,
        *,
        lane: PostgresConnectionLane,
        row_factory: object | None = None,
        deadline_monotonic: float,
        isolation_level: IsolationLevel = IsolationLevel.READ_COMMITTED,
    ) -> Iterator[Connection]:
        if lane not in PostgresConnectionLane:
            raise ValueError("unknown PostgreSQL connection lane")
        self._require_usable()
        try:
            connection = self._factory.connect(
                deadline_monotonic=deadline_monotonic, row_factory=row_factory
            )
            self._validate_physical_connection(connection)
            connection.isolation_level = isolation_level
            yield connection
            if not connection.closed and connection.info.transaction_status.name != "IDLE":
                connection.commit()
        except BaseException:
            if "connection" in locals() and not connection.closed and connection.info.transaction_status.name != "IDLE":
                connection.rollback()
            raise
        finally:
            if "connection" in locals() and not connection.closed:
                connection.close()

    def close(self) -> None:
        with self._lock:
            self._closing = True

    def _retain_borrower(self) -> None:
        with self._lock:
            self._require_usable()
            self._borrowers += 1

    def _release_borrower(self) -> None:
        with self._lock:
            self._borrowers = max(0, self._borrowers - 1)

    def _require_usable(self) -> None:
        with self._lock:
            if self._closing or self._invalidated:
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.ACCESS_LEASE_RELEASED,
                    "verified PostgreSQL provider is unavailable",
                )

    def _validate_physical_connection(self, connection: Connection) -> None:
        try:
            self._factory.validate_effective_endpoint(connection)
            identity = _read_identity_from_connection(connection)
            rows = read_migration_ledger(connection)
            if rows is None:
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.DATABASE_IDENTITY_MISMATCH,
                    "clean migration ledger disappeared",
                )
            _validate_clean_ledger(rows, POSTGRES_MIGRATION_REGISTRY.definition(0))
            mismatch = (
                identity.database_name != self._binding.database_name
                or identity.database_oid != self._binding.database_oid
                or identity.runtime_role != self._binding.runtime_role
                or identity.normalized_search_path != self._binding.normalized_search_path
                or identity.server_version_num != self._binding.server_version_num
                or rows[0].universe_fingerprint
                != self._binding.migration_universe_fingerprint
                or rows[0].registry_prefix_fingerprint
                != self._binding.durable_registry_prefix_fingerprint
            )
            if mismatch:
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.DATABASE_IDENTITY_MISMATCH,
                    "physical PostgreSQL connection differs from binding v2",
                )
            if connection.info.transaction_status.name != "IDLE":
                connection.rollback()
        except BaseException:
            with self._lock:
                self._invalidated = True
            if not connection.closed:
                connection.close()
            raise


def postgres_operation_deadline(
    deadline_monotonic: float | None, *, timeout_seconds: float = 30.0
) -> float:
    return deadline_monotonic if deadline_monotonic is not None else monotonic() + timeout_seconds


__all__ = [
    "BorrowedVerifiedPostgresConnectionProvider",
    "PostgresConnectionLane",
    "PostgresPreflightIdentity",
    "PostgresRuntimeConnectionFactory",
    "VerifiedPostgresConnectionProvider",
    "VerifiedPostgresConnectionProviderProtocol",
    "postgres_operation_deadline",
]
