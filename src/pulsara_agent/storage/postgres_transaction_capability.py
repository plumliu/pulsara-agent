"""Sealed same-transaction capability used by the canonical-memory UOW."""

from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from typing import Iterator

from psycopg import Connection

from pulsara_agent.ports.projection_jobs import (
    CanonicalMutationCommitPort,
    CanonicalMutationDriverAuthority,
    CanonicalMutationTransactionIdentity,
    MemoryUowPhysicalTransactionRequest,
    MemoryUowScopeFactoryAuthority,
    PostgresCanonicalMutationTransactionDriverPort,
)
from pulsara_agent.projection_jobs.contracts import (
    CanonicalMutationBundleAppendReceiptFact,
    PreparedCanonicalMutationBundleFact,
)


class PostgresMemoryUowPhysicalTransactionCapability:
    """Lexically bounded access to one admitted PostgreSQL transaction."""

    __slots__ = (
        "_connection",
        "_driver",
        "_identity",
        "_lock",
        "_request",
        "_scope_authority",
        "_active",
        "_borrow_count",
    )

    def __init__(
        self,
        *,
        connection: Connection,
        request: MemoryUowPhysicalTransactionRequest,
        transaction_identity: CanonicalMutationTransactionIdentity,
        scope_authority: MemoryUowScopeFactoryAuthority,
        driver: PostgresCanonicalMutationTransactionDriverPort,
    ) -> None:
        self._connection = connection
        self._request = request
        self._identity = transaction_identity
        self._scope_authority = scope_authority
        self._driver = driver
        self._lock = RLock()
        self._active = True
        self._borrow_count = 0

    @property
    def transaction_identity(self) -> CanonicalMutationTransactionIdentity:
        return self._identity

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @contextmanager
    def borrow_for_scope_factory(
        self, *, authority: MemoryUowScopeFactoryAuthority
    ) -> Iterator[Connection]:
        if authority is not self._scope_authority:
            raise PermissionError("memory UOW scope authority mismatch")
        with self._borrow() as connection:
            yield connection

    @contextmanager
    def borrow_for_mutation_driver(
        self, *, authority: CanonicalMutationDriverAuthority
    ) -> Iterator[Connection]:
        if authority is not self._driver.driver_authority:
            raise PermissionError("canonical mutation driver authority mismatch")
        with self._borrow() as connection:
            yield connection

    def issue_canonical_mutation_commit_port(
        self, *, authority: MemoryUowScopeFactoryAuthority
    ) -> CanonicalMutationCommitPort:
        if authority is not self._scope_authority:
            raise PermissionError("memory UOW scope authority mismatch")
        self._require_active()
        return _BoundCanonicalMutationCommitPort(
            capability=self,
            driver=self._driver,
        )

    def close(self) -> None:
        with self._lock:
            self._active = False
            if self._borrow_count:
                raise RuntimeError(
                    "memory UOW physical transaction still has active borrows"
                )

    @contextmanager
    def _borrow(self) -> Iterator[Connection]:
        with self._lock:
            self._require_active()
            if self._connection.closed:
                raise RuntimeError("memory UOW physical connection is closed")
            if self._connection.info.backend_pid != self._identity.backend_pid:
                raise RuntimeError("memory UOW backend identity changed")
            self._borrow_count += 1
        try:
            yield self._connection
        finally:
            with self._lock:
                self._borrow_count -= 1

    def _require_active(self) -> None:
        if not self._active:
            raise RuntimeError("memory UOW physical transaction is released")

    def __reduce__(self) -> object:
        raise TypeError("memory UOW physical capability is not serializable")


class _BoundCanonicalMutationCommitPort:
    __slots__ = ("_capability", "_driver")

    def __init__(
        self,
        *,
        capability: PostgresMemoryUowPhysicalTransactionCapability,
        driver: PostgresCanonicalMutationTransactionDriverPort,
    ) -> None:
        self._capability = capability
        self._driver = driver

    @property
    def transaction_identity(self) -> CanonicalMutationTransactionIdentity:
        return self._capability.transaction_identity

    def append_bundle(
        self, *, bundle: PreparedCanonicalMutationBundleFact
    ) -> CanonicalMutationBundleAppendReceiptFact:
        if not self._capability.active:
            raise RuntimeError("canonical mutation commit port is released")
        return self._driver.append_on_transaction(
            transaction=self._capability,
            bundle=bundle,
        )

    def __reduce__(self) -> object:
        raise TypeError("canonical mutation commit port is not serializable")


__all__ = ["PostgresMemoryUowPhysicalTransactionCapability"]
