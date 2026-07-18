"""Process-owned PostgreSQL pool used by runtime event ledgers."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout


_POOL_LOCK = Lock()
_POOLS: dict[str, ConnectionPool] = {}
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


def postgres_event_pool(dsn: str) -> ConnectionPool:
    """Return one bounded process-owned pool per canonical connection string."""

    with _POOL_LOCK:
        pool = _POOLS.get(dsn)
        if pool is None:
            pool = ConnectionPool(
                conninfo=dsn,
                min_size=0,
                max_size=_MAX_CONNECTIONS,
                timeout=_DEFAULT_LEASE_TIMEOUT_SECONDS,
                open=True,
                name="pulsara-event-log",
            )
            _POOLS[dsn] = pool
            _READ_LEASES[dsn] = BoundedSemaphore(
                _MAX_CONNECTIONS - _CRITICAL_WRITE_RESERVE
            )
        return pool


def close_postgres_event_pool(dsn: str) -> None:
    """Close and forget one process-owned pool after an isolated database run."""

    with _POOL_LOCK:
        pool = _POOLS.pop(dsn, None)
        _READ_LEASES.pop(dsn, None)
    if pool is not None:
        pool.close()


@contextmanager
def postgres_event_connection(
    dsn: str,
    *,
    lane: PostgresConnectionLane = PostgresConnectionLane.CRITICAL_WRITE,
    deadline_monotonic: float | None = None,
) -> Iterator[psycopg.Connection]:
    """Lease one connection while preserving capacity for durable writers."""

    pool = postgres_event_pool(dsn)
    remaining = _remaining_seconds(deadline_monotonic)
    read_lease = _READ_LEASES[dsn] if lane is PostgresConnectionLane.BOUNDED_READ else None
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
