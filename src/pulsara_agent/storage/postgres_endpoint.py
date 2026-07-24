"""Resolved, secret-owning PostgreSQL endpoint and physical operation control."""

from __future__ import annotations

from dataclasses import dataclass
import os
from threading import RLock, Timer
from time import monotonic
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from pulsara_agent.storage.migrations.contracts import postgres_schema_fingerprint
from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)


_ALLOWED_CONNINFO_KEYS = frozenset(
    {"host", "port", "dbname", "user", "password", "sslmode"}
)
_FORBIDDEN_CONNINFO_KEYS = frozenset(
    {
        "service",
        "servicefile",
        "options",
        "target_session_attrs",
        "hostaddr",
        "passfile",
        "sslkey",
    }
)
_FORBIDDEN_LIBPQ_ENVIRONMENT = frozenset(
    {"PGSERVICE", "PGSERVICEFILE", "PGOPTIONS", "PGTARGETSESSIONATTRS"}
)


@dataclass(frozen=True, slots=True)
class PostgresCanonicalEndpointFact:
    transport: str
    host: str
    port: str
    database: str
    sslmode: str
    endpoint_fingerprint: str


class PostgresPhysicalOperationControl:
    """Own cancellation for one deadline-bounded physical DB operation."""

    def __init__(self, *, deadline_monotonic: float) -> None:
        self.deadline_monotonic = deadline_monotonic
        self._lock = RLock()
        self._connection: Connection | None = None
        self._timer: Timer | None = None
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def arm(self) -> None:
        remaining = _remaining(self.deadline_monotonic)
        with self._lock:
            if self._timer is not None:
                return
            timer = Timer(remaining, self.cancel)
            timer.daemon = True
            timer.name = "pulsara-postgres-operation-deadline"
            self._timer = timer
            timer.start()

    def bind(self, connection: Connection) -> None:
        with self._lock:
            if self._cancelled:
                connection.close()
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.DEADLINE_EXCEEDED,
                    "PostgreSQL physical operation deadline exceeded",
                    retryable=True,
                )
            self._connection = connection

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            connection = self._connection
        if connection is None or connection.closed:
            return
        try:
            connection.cancel_safe(timeout=0.5)
        except BaseException:
            pass
        try:
            connection.close()
        except BaseException:
            pass

    def finish(self) -> None:
        with self._lock:
            timer = self._timer
            self._timer = None
            self._connection = None
        if timer is not None:
            timer.cancel()


class ResolvedPostgresConnectionFactory:
    """The only owner allowed to retain conninfo and call psycopg.connect()."""

    def __init__(self, dsn: str, *, application_name: str) -> None:
        parameters = _validate_conninfo(dsn)
        parameters.setdefault("port", "5432")
        parameters.setdefault("sslmode", "prefer")
        parameters["application_name"] = application_name
        self._parameters = parameters
        self._dsn = make_conninfo(**parameters)
        endpoint_payload = {
            "transport": (
                "unix" if str(parameters.get("host", "")).startswith("/") else "tcp"
            ),
            "host": str(parameters["host"]),
            "port": str(parameters["port"]),
            "database": str(parameters["dbname"]),
            "sslmode": str(parameters["sslmode"]),
        }
        self.endpoint = PostgresCanonicalEndpointFact(
            **endpoint_payload,
            endpoint_fingerprint=postgres_schema_fingerprint(
                "pulsara:postgres-database-target:v1", endpoint_payload
            ),
        )
        self.application_name = application_name
        self.expected_role = str(parameters["user"])

    def connect(
        self,
        *,
        deadline_monotonic: float,
        row_factory: object | None = None,
        autocommit: bool = False,
        operation_control: PostgresPhysicalOperationControl | None = None,
    ) -> Connection:
        remaining = _remaining(deadline_monotonic)
        kwargs: dict[str, Any] = {
            "connect_timeout": max(1, int(remaining)),
            "autocommit": True,
        }
        if row_factory is not None:
            kwargs["row_factory"] = row_factory
        connection: Connection | None = None
        try:
            connection = psycopg.connect(self._dsn, **kwargs)
            if operation_control is not None:
                operation_control.bind(connection)
            self.validate_effective_endpoint(connection)
            apply_connection_deadline(connection, deadline_monotonic)
            connection.autocommit = autocommit
            return connection
        except PostgresSchemaError:
            if connection is not None and not connection.closed:
                connection.close()
            raise
        except BaseException as exc:
            if connection is not None and not connection.closed:
                connection.close()
            raise classify_postgres_physical_failure(
                exc,
                deadline_monotonic=deadline_monotonic,
                operation_control=operation_control,
            ) from exc

    def pool_conninfo(self, *, connect_timeout_seconds: float) -> str:
        return make_conninfo(
            self._dsn,
            connect_timeout=max(1, int(connect_timeout_seconds)),
        )

    def validate_effective_endpoint(self, connection: Connection) -> None:
        effective = connection.info.get_parameters()
        observed = {
            "host": connection.info.host,
            "port": str(connection.info.port),
            "dbname": connection.info.dbname,
            "user": connection.info.user,
            "sslmode": effective.get("sslmode", "prefer"),
            "application_name": effective.get("application_name"),
        }
        expected = {
            "host": self.endpoint.host,
            "port": self.endpoint.port,
            "dbname": self.endpoint.database,
            "user": self.expected_role,
            "sslmode": self.endpoint.sslmode,
            "application_name": self.application_name,
        }
        mismatches = tuple(
            field
            for field in expected
            if str(observed[field]) != str(expected[field])
        )
        if mismatches:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.DATABASE_IDENTITY_MISMATCH,
                "effective PostgreSQL endpoint differs from resolved authority: "
                + ", ".join(mismatches),
            )


def apply_connection_deadline(
    connection: Connection, deadline_monotonic: float
) -> None:
    milliseconds = max(1, int(_remaining(deadline_monotonic) * 1000))
    restore_transactional_mode = not connection.autocommit
    if restore_transactional_mode:
        connection.autocommit = True
    try:
        connection.execute(
            "SELECT pg_catalog.set_config('statement_timeout', %s, false)",
            (f"{milliseconds}ms",),
        )
        connection.execute(
            "SELECT pg_catalog.set_config('lock_timeout', %s, false)",
            (f"{milliseconds}ms",),
        )
    finally:
        if restore_transactional_mode and not connection.closed:
            connection.autocommit = False


def classify_postgres_physical_failure(
    exc: BaseException,
    *,
    deadline_monotonic: float,
    operation_control: PostgresPhysicalOperationControl | None,
) -> PostgresSchemaError:
    deadline_expired = monotonic() >= deadline_monotonic
    cancelled = operation_control is not None and operation_control.cancelled
    if deadline_expired or cancelled or isinstance(exc, psycopg.errors.QueryCanceled):
        return PostgresSchemaError(
            PostgresSchemaFailureCode.DEADLINE_EXCEEDED,
            "PostgreSQL physical operation deadline exceeded",
            retryable=True,
        )
    return PostgresSchemaError(
        PostgresSchemaFailureCode.CONNECTION_FAILED,
        f"PostgreSQL physical operation failed: {type(exc).__name__}",
        retryable=True,
    )


def _validate_conninfo(dsn: str) -> dict[str, str]:
    if not dsn.strip():
        raise ValueError("PostgreSQL DSN must be non-empty")
    inherited = sorted(
        name for name in _FORBIDDEN_LIBPQ_ENVIRONMENT if os.getenv(name)
    )
    if inherited:
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.CONNINFO_UNSUPPORTED,
            "authority-bearing libpq environment is unsupported: "
            + ", ".join(inherited),
        )
    try:
        parameters = conninfo_to_dict(dsn)
    except Exception as exc:
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.CONNINFO_UNSUPPORTED,
            "PostgreSQL conninfo could not be parsed under the V1 authority contract",
        ) from exc
    keys = frozenset(parameters)
    forbidden = sorted(keys & _FORBIDDEN_CONNINFO_KEYS)
    unknown = sorted(keys - _ALLOWED_CONNINFO_KEYS)
    if forbidden or unknown:
        detail = ", ".join((*forbidden, *unknown))
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.CONNINFO_UNSUPPORTED,
            f"unsupported PostgreSQL conninfo parameters: {detail}",
        )
    host = str(parameters.get("host", ""))
    port = str(parameters.get("port", "5432"))
    if "," in host or "," in port:
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.CONNINFO_UNSUPPORTED,
            "multi-host PostgreSQL conninfo is not supported",
        )
    if not parameters.get("host") or not parameters.get("dbname") or not parameters.get("user"):
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.CONNINFO_UNSUPPORTED,
            "PostgreSQL conninfo must explicitly name host, database, and user",
        )
    return {str(key): str(value) for key, value in parameters.items()}


def _remaining(deadline_monotonic: float) -> float:
    remaining = deadline_monotonic - monotonic()
    if remaining <= 0:
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.DEADLINE_EXCEEDED,
            "PostgreSQL connection deadline exceeded",
            retryable=True,
        )
    return remaining


__all__ = [
    "PostgresCanonicalEndpointFact",
    "PostgresPhysicalOperationControl",
    "ResolvedPostgresConnectionFactory",
    "apply_connection_deadline",
    "classify_postgres_physical_failure",
]
