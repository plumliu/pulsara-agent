"""Sealed physical transaction capability for projection migrations."""

from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from typing import Iterator

from psycopg import Connection

from pulsara_agent.ports.projection_jobs import (
    ProjectionMigrationPortAuthority,
    ProjectionMigrationTransactionIdentity,
)


class PostgresProjectionMigrationTransactionCapability:
    __slots__ = ("_active", "_authority", "_connection", "_identity", "_lock")

    def __init__(
        self,
        *,
        connection: Connection,
        identity: ProjectionMigrationTransactionIdentity,
        authority: ProjectionMigrationPortAuthority,
    ) -> None:
        self._connection = connection
        self._identity = identity
        self._authority = authority
        self._active = True
        self._lock = RLock()

    @property
    def transaction_identity(self) -> ProjectionMigrationTransactionIdentity:
        return self._identity

    def assert_active(self) -> None:
        with self._lock:
            if not self._active or self._connection.closed:
                raise RuntimeError("projection migration transaction is released")
            if self._connection.info.backend_pid != self._identity.backend_pid:
                raise RuntimeError("projection migration backend identity changed")

    @contextmanager
    def borrow_for_port(
        self, *, authority: ProjectionMigrationPortAuthority
    ) -> Iterator[Connection]:
        if authority is not self._authority:
            raise PermissionError("projection migration port authority mismatch")
        self.assert_active()
        yield self._connection
        self.assert_active()

    def release(self) -> None:
        with self._lock:
            self._active = False

    def __reduce__(self) -> object:
        raise TypeError("projection migration transaction is not serializable")


__all__ = ["PostgresProjectionMigrationTransactionCapability"]
