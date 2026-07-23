"""Verifier-owned PostgreSQL physical connection authority."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from threading import RLock, Thread
from time import monotonic
from typing import Callable, ContextManager, Iterator, Protocol

from psycopg import Connection
from psycopg_pool import ConnectionPool

from pulsara_agent.storage.migrations.contracts import postgres_schema_fingerprint
from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)
from pulsara_agent.storage.migrations.runner import (
    PostgresDatabaseIdentity,
    _read_identity_from_connection,
    read_migration_ledger,
)
from pulsara_agent.storage.migrations.verifier import (
    PostgresDeepVerificationBundle,
    PostgresFastVerificationBundle,
    PostgresMigrationHistoryStatus,
    PostgresSchemaVerifier,
    classify_migration_history,
)
from pulsara_agent.storage.postgres_endpoint import (
    PostgresPhysicalOperationControl,
    ResolvedPostgresConnectionFactory,
    apply_connection_deadline,
    classify_postgres_physical_failure,
)
from pulsara_agent.storage.schema_contract import VerifiedPostgresSchemaBinding


class PostgresConnectionLane(StrEnum):
    EVENT_LOG = "event_log"
    ARTIFACT = "artifact"
    HOST_CONTROL = "host_control"
    MEMORY_UOW = "memory_uow"
    MEMORY_QUERY = "memory_query"
    MEMORY_MAINTENANCE = "memory_maintenance"
    GOVERNANCE = "governance"
    INSPECTOR = "inspector"
    CHECKPOINT_MAINTENANCE = "checkpoint_maintenance"


@dataclass(frozen=True, slots=True)
class PostgresPoolPolicyFact:
    lane: PostgresConnectionLane
    min_size: int
    max_size: int
    max_waiting: int
    connect_timeout_seconds: float
    checkout_timeout_seconds: float
    policy_fingerprint: str


def _pool_policy(
    lane: PostgresConnectionLane,
    *,
    min_size: int,
    max_size: int,
    max_waiting: int,
    connect_timeout_seconds: float = 10.0,
    checkout_timeout_seconds: float = 30.0,
) -> PostgresPoolPolicyFact:
    payload = {
        "lane": lane.value,
        "min_size": min_size,
        "max_size": max_size,
        "max_waiting": max_waiting,
        "connect_timeout_seconds": str(connect_timeout_seconds),
        "checkout_timeout_seconds": str(checkout_timeout_seconds),
    }
    return PostgresPoolPolicyFact(
        lane=lane,
        min_size=min_size,
        max_size=max_size,
        max_waiting=max_waiting,
        connect_timeout_seconds=connect_timeout_seconds,
        checkout_timeout_seconds=checkout_timeout_seconds,
        policy_fingerprint=postgres_schema_fingerprint(
            "pulsara:postgres-pool-policy:v1", payload
        ),
    )


POSTGRES_POOL_POLICIES = {
    PostgresConnectionLane.EVENT_LOG: _pool_policy(
        PostgresConnectionLane.EVENT_LOG,
        min_size=0,
        max_size=16,
        max_waiting=64,
    ),
    PostgresConnectionLane.ARTIFACT: _pool_policy(
        PostgresConnectionLane.ARTIFACT, min_size=0, max_size=4, max_waiting=16
    ),
    PostgresConnectionLane.HOST_CONTROL: _pool_policy(
        PostgresConnectionLane.HOST_CONTROL, min_size=0, max_size=4, max_waiting=16
    ),
    PostgresConnectionLane.MEMORY_UOW: _pool_policy(
        PostgresConnectionLane.MEMORY_UOW, min_size=0, max_size=8, max_waiting=32
    ),
    PostgresConnectionLane.MEMORY_QUERY: _pool_policy(
        PostgresConnectionLane.MEMORY_QUERY, min_size=0, max_size=8, max_waiting=32
    ),
    PostgresConnectionLane.MEMORY_MAINTENANCE: _pool_policy(
        PostgresConnectionLane.MEMORY_MAINTENANCE,
        min_size=0,
        max_size=4,
        max_waiting=16,
    ),
    PostgresConnectionLane.GOVERNANCE: _pool_policy(
        PostgresConnectionLane.GOVERNANCE, min_size=0, max_size=8, max_waiting=32
    ),
    PostgresConnectionLane.INSPECTOR: _pool_policy(
        PostgresConnectionLane.INSPECTOR, min_size=0, max_size=4, max_waiting=16
    ),
    PostgresConnectionLane.CHECKPOINT_MAINTENANCE: _pool_policy(
        PostgresConnectionLane.CHECKPOINT_MAINTENANCE,
        min_size=0,
        max_size=2,
        max_waiting=8,
    ),
}


@dataclass(frozen=True, slots=True)
class PostgresPreflightIdentity:
    database_target_fingerprint: str
    database_identity: PostgresDatabaseIdentity


class PostgresRuntimeConnectionFactory:
    """Runtime-role wrapper over the sole resolved endpoint owner."""

    def __init__(self, dsn: str) -> None:
        self._resolved = ResolvedPostgresConnectionFactory(
            dsn,
            application_name="pulsara-runtime",
        )
        self.database_target_fingerprint = self._resolved.endpoint.endpoint_fingerprint

    @property
    def runtime_role(self) -> str:
        return self._resolved.expected_role

    def connect(
        self,
        *,
        deadline_monotonic: float,
        row_factory: object | None = None,
        autocommit: bool = False,
    ) -> Connection:
        return self._resolved.connect(
            deadline_monotonic=deadline_monotonic,
            row_factory=row_factory,
            autocommit=autocommit,
        )

    def pool_conninfo(self, *, connect_timeout_seconds: float) -> str:
        return self._resolved.pool_conninfo(
            connect_timeout_seconds=connect_timeout_seconds
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
        control.arm()
        try:
            connection = self._resolved.connect(
                deadline_monotonic=deadline_monotonic,
                autocommit=True,
                operation_control=control,
            )
            with connection:
                identity = _read_identity_from_connection(connection)
        except PostgresSchemaError:
            raise
        except BaseException as exc:
            raise classify_postgres_physical_failure(
                exc,
                deadline_monotonic=deadline_monotonic,
                operation_control=control,
            ) from exc
        finally:
            control.finish()
        return PostgresPreflightIdentity(
            database_target_fingerprint=self.database_target_fingerprint,
            database_identity=identity,
        )

    def verify(
        self,
        *,
        deadline_monotonic: float,
        operation_control: PostgresPhysicalOperationControl | None = None,
    ) -> PostgresFastVerificationBundle:
        control = operation_control or PostgresPhysicalOperationControl(
            deadline_monotonic=deadline_monotonic
        )
        control.arm()
        try:
            connection = self._resolved.connect(
                deadline_monotonic=deadline_monotonic,
                autocommit=True,
                operation_control=control,
            )
            with connection:
                return PostgresSchemaVerifier().verify_fast_connection(
                    connection,
                    database_target_fingerprint=self.database_target_fingerprint,
                    deadline_monotonic=deadline_monotonic,
                )
        except PostgresSchemaError:
            raise
        except BaseException as exc:
            raise classify_postgres_physical_failure(
                exc,
                deadline_monotonic=deadline_monotonic,
                operation_control=control,
            ) from exc
        finally:
            control.finish()

    def verify_deep(
        self,
        *,
        deadline_monotonic: float,
        operation_control: PostgresPhysicalOperationControl | None = None,
    ) -> PostgresDeepVerificationBundle:
        control = operation_control or PostgresPhysicalOperationControl(
            deadline_monotonic=deadline_monotonic
        )
        control.arm()
        try:
            connection = self._resolved.connect(
                deadline_monotonic=deadline_monotonic,
                autocommit=True,
                operation_control=control,
            )
            with connection:
                return PostgresSchemaVerifier().verify_deep_connection(
                    connection,
                    database_target_fingerprint=self.database_target_fingerprint,
                    deadline_monotonic=deadline_monotonic,
                )
        except PostgresSchemaError:
            raise
        except BaseException as exc:
            raise classify_postgres_physical_failure(
                exc,
                deadline_monotonic=deadline_monotonic,
                operation_control=control,
            ) from exc
        finally:
            control.finish()


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
    ) -> ContextManager[Connection]: ...

    def pool(
        self,
        *,
        lane: PostgresConnectionLane,
        deadline_monotonic: float,
    ) -> "VerifiedPostgresPoolLease": ...

    def close_pool(self, lane: PostgresConnectionLane) -> None: ...


class VerifiedPostgresPoolLease:
    def __init__(
        self,
        *,
        pool: ConnectionPool,
        policy: PostgresPoolPolicyFact,
        usability_guard: Callable[[], None],
    ) -> None:
        self._pool = pool
        self.policy = policy
        self._usability_guard = usability_guard

    @contextmanager
    def connection(self, *, timeout: float) -> Iterator[Connection]:
        self._usability_guard()
        with self._pool.connection(timeout=timeout) as connection:
            yield connection

    def scoped_to(
        self, usability_guard: Callable[[], None]
    ) -> "VerifiedPostgresPoolLease":
        def combined_guard() -> None:
            self._usability_guard()
            usability_guard()

        return VerifiedPostgresPoolLease(
            pool=self._pool,
            policy=self.policy,
            usability_guard=combined_guard,
        )


class BorrowedVerifiedPostgresConnectionProvider:
    """Borrower-scoped facade invalidated with its access lease."""

    def __init__(self, provider: "VerifiedPostgresConnectionProvider") -> None:
        self._provider = provider
        self._lock = RLock()
        self._released = False
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
    def connection(
        self,
        *,
        lane: PostgresConnectionLane,
        row_factory: object | None = None,
        deadline_monotonic: float,
    ) -> Iterator[Connection]:
        self._require_active()
        with self._provider.connection(
            lane=lane,
            row_factory=row_factory,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            yield connection

    def pool(
        self,
        *,
        lane: PostgresConnectionLane,
        deadline_monotonic: float,
    ) -> VerifiedPostgresPoolLease:
        self._require_active()
        return self._provider.pool(
            lane=lane,
            deadline_monotonic=deadline_monotonic,
        ).scoped_to(self._require_active)

    def close_pool(self, lane: PostgresConnectionLane) -> None:
        self._require_active()
        self._provider.close_pool(lane)

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
                    "verified PostgreSQL borrower access lease is released",
                )

    def __reduce__(self) -> object:
        raise TypeError("borrowed PostgreSQL connection provider is not serializable")


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
        self._pools: dict[PostgresConnectionLane, ConnectionPool] = {}
        self._active_lease_count = 0
        self._released = False
        self._invalidated = False

    @property
    def schema_binding(self) -> VerifiedPostgresSchemaBinding:
        return self._binding

    @property
    def verification_observed_at_utc(self) -> str:
        return self._verification_observed_at_utc

    def borrow(self) -> BorrowedVerifiedPostgresConnectionProvider:
        return BorrowedVerifiedPostgresConnectionProvider(self)

    def _retain_borrower(self) -> None:
        with self._lock:
            self._require_usable()
            self._active_lease_count += 1

    def _release_borrower(self) -> None:
        pools: tuple[ConnectionPool, ...] = ()
        with self._lock:
            if self._active_lease_count <= 0:
                return
            self._active_lease_count -= 1
            if self._active_lease_count == 0 and self._released:
                pools = tuple(self._pools.values())
                self._pools.clear()
        for pool in pools:
            pool.close()

    def close(self) -> None:
        with self._lock:
            self._released = True
            pools = tuple(self._pools.values()) if self._active_lease_count == 0 else ()
            if pools:
                self._pools.clear()
        for pool in pools:
            pool.close()

    @contextmanager
    def connection(
        self,
        *,
        lane: PostgresConnectionLane,
        row_factory: object | None = None,
        deadline_monotonic: float,
    ) -> Iterator[Connection]:
        del lane
        with self._lock:
            self._require_usable()
        try:
            connection = self._factory.connect(
                deadline_monotonic=deadline_monotonic,
                row_factory=row_factory,
            )
        except PostgresSchemaError as exc:
            if exc.code is PostgresSchemaFailureCode.DATABASE_IDENTITY_MISMATCH:
                self._invalidate_confirmed_physical_binding()
            raise
        try:
            self._validate_physical_connection(
                connection,
                deadline_monotonic=deadline_monotonic,
            )
            yield connection
            if (
                not connection.closed
                and connection.info.transaction_status.name != "IDLE"
            ):
                connection.commit()
        except BaseException:
            if (
                not connection.closed
                and connection.info.transaction_status.name != "IDLE"
            ):
                connection.rollback()
            raise
        finally:
            if not connection.closed:
                connection.close()

    def pool(
        self,
        *,
        lane: PostgresConnectionLane,
        deadline_monotonic: float,
    ) -> VerifiedPostgresPoolLease:
        _remaining(deadline_monotonic)
        with self._lock:
            self._require_usable()
            pool = self._pools.get(lane)
            policy = POSTGRES_POOL_POLICIES[lane]
            if pool is None:
                pool = ConnectionPool(
                    conninfo=self._factory.pool_conninfo(
                        connect_timeout_seconds=policy.connect_timeout_seconds
                    ),
                    min_size=policy.min_size,
                    max_size=policy.max_size,
                    max_waiting=policy.max_waiting,
                    timeout=policy.checkout_timeout_seconds,
                    configure=self._configure_pool_connection,
                    open=True,
                    name=f"pulsara-{lane.value}",
                )
                self._pools[lane] = pool
            return VerifiedPostgresPoolLease(
                pool=pool,
                policy=policy,
                usability_guard=self._require_usable,
            )

    def close_pool(self, lane: PostgresConnectionLane) -> None:
        with self._lock:
            pool = self._pools.pop(lane, None)
        if pool is not None:
            pool.close()

    def _configure_pool_connection(self, connection: Connection) -> None:
        deadline_monotonic = monotonic() + 30.0
        apply_connection_deadline(connection, deadline_monotonic)
        self._validate_physical_connection(
            connection,
            deadline_monotonic=deadline_monotonic,
        )

    def _validate_physical_connection(
        self,
        connection: Connection,
        *,
        deadline_monotonic: float,
    ) -> None:
        try:
            self._factory.validate_effective_endpoint(connection)
            identity = _read_identity_from_connection(connection)
            rows = read_migration_ledger(connection)
            history_status = classify_migration_history(rows)
            identity_mismatch = (
                identity.database_name != self._binding.database_name
                or identity.database_oid != self._binding.database_oid
                or identity.runtime_role != self._binding.runtime_role
                or identity.normalized_search_path
                != self._binding.normalized_search_path
                or identity.server_version_num != self._binding.server_version_num
            )
            ledger_matches = (
                rows is not None
                and bool(rows)
                and history_status is PostgresMigrationHistoryStatus.UP_TO_DATE
                and rows[-1].version == self._binding.migration_head_version
                and rows[-1].registry_prefix_fingerprint
                == self._binding.durable_registry_prefix_fingerprint
            )
            if identity_mismatch or not ledger_matches:
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.DATABASE_IDENTITY_MISMATCH,
                    "physical PostgreSQL connection does not match verified binding",
                )
            if connection.info.transaction_status.name != "IDLE":
                connection.rollback()
        except PostgresSchemaError as exc:
            if exc.code in {
                PostgresSchemaFailureCode.DATABASE_IDENTITY_MISMATCH,
                PostgresSchemaFailureCode.SEARCH_PATH_MISMATCH,
                PostgresSchemaFailureCode.SERVER_VERSION_UNSUPPORTED,
            }:
                _close_probe_connection(connection)
                self._invalidate_confirmed_physical_binding()
                raise PostgresSchemaError(
                    PostgresSchemaFailureCode.DATABASE_IDENTITY_MISMATCH,
                    "physical PostgreSQL connection failed verified binding validation",
                ) from exc
            _close_probe_connection(connection)
            raise
        except BaseException as exc:
            _close_probe_connection(connection)
            raise classify_postgres_physical_failure(
                exc,
                deadline_monotonic=deadline_monotonic,
                operation_control=None,
            ) from exc

    def _invalidate_confirmed_physical_binding(self) -> None:
        with self._lock:
            self._invalidated = True
            pools = tuple(self._pools.values())
            self._pools.clear()
        _retire_invalidated_pools(pools)

    def _require_usable(self) -> None:
        if self._released or self._invalidated:
            raise PostgresSchemaError(
                PostgresSchemaFailureCode.ACCESS_LEASE_RELEASED,
                "verified PostgreSQL connection provider is unavailable",
            )


def _retire_invalidated_pools(pools: tuple[ConnectionPool, ...]) -> None:
    if not pools:
        return

    def retire() -> None:
        for pool in pools:
            pool.close()

    Thread(
        target=retire,
        name="pulsara-postgres-pool-retirement",
        daemon=True,
    ).start()


def _close_probe_connection(connection: Connection) -> None:
    if connection.closed:
        return
    try:
        if connection.info.transaction_status.name != "IDLE":
            connection.rollback()
    except BaseException:
        pass
    try:
        connection.close()
    except BaseException:
        pass


def _remaining(deadline_monotonic: float) -> float:
    remaining = deadline_monotonic - monotonic()
    if remaining <= 0:
        raise PostgresSchemaError(
            PostgresSchemaFailureCode.DEADLINE_EXCEEDED,
            "PostgreSQL connection deadline exceeded",
            retryable=True,
        )
    return remaining


def postgres_operation_deadline(
    deadline_monotonic: float | None,
    *,
    timeout_seconds: float = 30.0,
) -> float:
    """Resolve a boundary deadline when an older protocol has no deadline field."""

    return (
        deadline_monotonic
        if deadline_monotonic is not None
        else monotonic() + timeout_seconds
    )


__all__ = [
    "BorrowedVerifiedPostgresConnectionProvider",
    "POSTGRES_POOL_POLICIES",
    "PostgresConnectionLane",
    "PostgresPoolPolicyFact",
    "PostgresPreflightIdentity",
    "PostgresRuntimeConnectionFactory",
    "VerifiedPostgresConnectionProvider",
    "VerifiedPostgresConnectionProviderProtocol",
    "VerifiedPostgresPoolLease",
    "postgres_operation_deadline",
]
