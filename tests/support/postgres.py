"""Verified PostgreSQL authority for adapter integration tests."""

from __future__ import annotations

import atexit
from contextlib import AbstractContextManager
from functools import lru_cache
from time import monotonic
from typing import NoReturn

import psycopg

from pulsara_agent.storage.postgres_connection_provider import (
    BorrowedVerifiedPostgresConnectionProvider,
)
from pulsara_agent.storage.schema_verification_service import (
    VerifiedPostgresAccessLease,
    acquire_verified_postgres_access_sync,
)


_LEASES: list[VerifiedPostgresAccessLease] = []


class UnverifiedTestPostgresConnectionProvider:
    """Test-only marker that rejects every attempted physical database access."""

    schema_binding = object()
    verification_observed_at_utc = "test-unverified"

    def retain(self) -> None:
        return None

    def release(self) -> None:
        return None

    def close(self) -> None:
        return None

    def close_pool(self, _lane: object) -> None:
        return None

    def connection(self, **_kwargs: object) -> AbstractContextManager[object]:
        self._reject_physical_access()

    def pool(self, **_kwargs: object) -> NoReturn:
        self._reject_physical_access()

    @staticmethod
    def _reject_physical_access() -> NoReturn:
        raise AssertionError(
            "unit test attempted PostgreSQL access through an unverified provider"
        )


class UnverifiedTestPostgresAccessLease:
    """Test-only lease for composition tests whose durable ports are replaced."""

    def __init__(self) -> None:
        self.connection_provider = UnverifiedTestPostgresConnectionProvider()
        self.released = False

    def release(self) -> None:
        self.released = True


def unverified_test_postgres_access_lease() -> UnverifiedTestPostgresAccessLease:
    return UnverifiedTestPostgresAccessLease()


@lru_cache(maxsize=16)
def verified_postgres_access_lease(dsn: str) -> VerifiedPostgresAccessLease:
    lease = acquire_verified_postgres_access_sync(
        dsn,
        deadline_monotonic=monotonic() + 30.0,
    )
    _LEASES.append(lease)
    return lease


def verified_postgres_provider(dsn: str) -> BorrowedVerifiedPostgresConnectionProvider:
    return verified_postgres_access_lease(dsn).connection_provider


def connect_postgres_test_database(
    dsn: str,
    *,
    autocommit: bool = False,
) -> psycopg.Connection:
    """Open the fixture-owned database; availability is owned by its fixture."""

    return psycopg.connect(dsn, connect_timeout=2, autocommit=autocommit)


def _release_test_leases() -> None:
    for lease in reversed(_LEASES):
        lease.release()
    _LEASES.clear()


atexit.register(_release_test_leases)


__all__ = [
    "UnverifiedTestPostgresAccessLease",
    "UnverifiedTestPostgresConnectionProvider",
    "connect_postgres_test_database",
    "unverified_test_postgres_access_lease",
    "verified_postgres_access_lease",
    "verified_postgres_provider",
]
