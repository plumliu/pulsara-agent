"""Bounded EventLog leases backed by verified PostgreSQL connection authority."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import Iterator

from psycopg import Connection
from psycopg_pool import PoolTimeout

from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane as PhysicalConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


_POOL_LOCK = Lock()
_READ_LEASES: dict[str, BoundedSemaphore] = {}
_MAX_CONNECTIONS = 16
_CRITICAL_WRITE_RESERVE = 4
_DEFAULT_LEASE_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class PostgresEventPoolCapacity:
    max_connections: int
    critical_write_reserve: int
    bounded_read_capacity: int
    default_lease_timeout_seconds: float


def postgres_event_pool_capacity() -> PostgresEventPoolCapacity:
    """Return the process-owned pool configuration for diagnostics."""

    return PostgresEventPoolCapacity(
        max_connections=_MAX_CONNECTIONS,
        critical_write_reserve=_CRITICAL_WRITE_RESERVE,
        bounded_read_capacity=_MAX_CONNECTIONS - _CRITICAL_WRITE_RESERVE,
        default_lease_timeout_seconds=_DEFAULT_LEASE_TIMEOUT_SECONDS,
    )


class PostgresConnectionLane(StrEnum):
    CRITICAL_WRITE = "critical_write"
    BOUNDED_READ = "bounded_read"


def postgres_event_pool(
    provider: VerifiedPostgresConnectionProviderProtocol,
    *,
    deadline_monotonic: float,
):
    """Borrow the provider-owned EventLog pool."""

    return provider.pool(
        lane=PhysicalConnectionLane.EVENT_LOG,
        deadline_monotonic=deadline_monotonic,
    )


def close_postgres_event_pool(
    provider: VerifiedPostgresConnectionProviderProtocol,
) -> None:
    """Close one isolated provider's EventLog pool."""

    key = provider.schema_binding.binding_fingerprint
    with _POOL_LOCK:
        _READ_LEASES.pop(key, None)
    provider.close_pool(PhysicalConnectionLane.EVENT_LOG)


@contextmanager
def postgres_event_connection(
    provider: VerifiedPostgresConnectionProviderProtocol,
    *,
    lane: PostgresConnectionLane = PostgresConnectionLane.CRITICAL_WRITE,
    deadline_monotonic: float | None = None,
) -> Iterator[Connection]:
    """Lease one connection while preserving capacity for durable writers."""

    deadline = deadline_monotonic or (monotonic() + _DEFAULT_LEASE_TIMEOUT_SECONDS)
    pool = postgres_event_pool(provider, deadline_monotonic=deadline)
    remaining = _remaining_seconds(deadline_monotonic)
    key = provider.schema_binding.binding_fingerprint
    with _POOL_LOCK:
        read_capacity = _READ_LEASES.setdefault(
            key,
            BoundedSemaphore(_MAX_CONNECTIONS - _CRITICAL_WRITE_RESERVE),
        )
    read_lease = read_capacity if lane is PostgresConnectionLane.BOUNDED_READ else None
    acquired = False
    try:
        if read_lease is not None:
            acquired = read_lease.acquire(timeout=remaining)
            if not acquired:
                raise TimeoutError("PostgreSQL bounded-read lease deadline exceeded")
            remaining = _remaining_seconds(deadline_monotonic)
        try:
            with pool.connection(timeout=remaining) as connection:
                yield connection
        except PoolTimeout as exc:
            raise TimeoutError("PostgreSQL connection lease deadline exceeded") from exc
    finally:
        if acquired and read_lease is not None:
            read_lease.release()


def _remaining_seconds(deadline_monotonic: float | None) -> float:
    if deadline_monotonic is None:
        return _DEFAULT_LEASE_TIMEOUT_SECONDS
    remaining = deadline_monotonic - monotonic()
    if remaining <= 0:
        raise TimeoutError("PostgreSQL connection deadline exceeded")
    return remaining


__all__ = [
    "PostgresConnectionLane",
    "PostgresEventPoolCapacity",
    "close_postgres_event_pool",
    "postgres_event_connection",
    "postgres_event_pool",
    "postgres_event_pool_capacity",
]
