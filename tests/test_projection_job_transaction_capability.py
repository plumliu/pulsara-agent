from __future__ import annotations

from typing import cast

import pytest

from pulsara_agent.ports.projection_jobs import (
    CanonicalMutationDriverAuthority,
    build_canonical_mutation_transaction_identity,
    build_memory_uow_physical_transaction_request,
    issue_canonical_mutation_driver_authority,
    issue_memory_uow_scope_factory_authority,
)
from pulsara_agent.projection_jobs.contracts import (
    CanonicalMutationBundleAppendReceiptFact,
    PreparedCanonicalMutationBundleFact,
)
from pulsara_agent.storage.postgres_transaction_capability import (
    PostgresMemoryUowPhysicalTransactionCapability,
)


class _Info:
    backend_pid = 73


class _Connection:
    closed = False
    info = _Info()


class _Driver:
    def __init__(self) -> None:
        self._authority = issue_canonical_mutation_driver_authority()
        self.connection = None

    @property
    def driver_authority(self) -> CanonicalMutationDriverAuthority:
        return self._authority

    def append_on_transaction(self, *, transaction, bundle):
        del bundle
        with transaction.borrow_for_mutation_driver(
            authority=self._authority
        ) as connection:
            self.connection = connection
        return cast(CanonicalMutationBundleAppendReceiptFact, object())


def _capability():
    connection = _Connection()
    driver = _Driver()
    scope_authority = issue_memory_uow_scope_factory_authority()
    identity = build_canonical_mutation_transaction_identity(
        schema_binding_fingerprint="sha256:schema",
        connection_provider_borrower_id="borrower:test",
        transaction_owner_id="uow:test",
        transaction_generation=1,
        backend_pid=connection.info.backend_pid,
        admission_epoch_fingerprint="sha256:epoch",
        admission_guard_lock_identity_fingerprint="sha256:guard",
    )
    request = build_memory_uow_physical_transaction_request(
        transaction_owner_id="uow:test",
        transaction_generation=1,
        deadline_monotonic=100.0,
        scope_request_fingerprint="sha256:scope",
    )
    capability = PostgresMemoryUowPhysicalTransactionCapability(
        connection=cast(object, connection),
        request=request,
        transaction_identity=identity,
        scope_authority=scope_authority,
        driver=driver,
    )
    return connection, driver, scope_authority, capability


def test_commit_port_uses_the_exact_admitted_physical_transaction() -> None:
    connection, driver, scope_authority, capability = _capability()
    port = capability.issue_canonical_mutation_commit_port(authority=scope_authority)

    receipt = port.append_bundle(
        bundle=cast(PreparedCanonicalMutationBundleFact, object())
    )

    assert driver.connection is connection
    assert receipt is not None
    assert port.transaction_identity == capability.transaction_identity


def test_transaction_capability_rejects_wrong_authority_and_post_release_use() -> None:
    _connection, _driver, _scope_authority, capability = _capability()
    with pytest.raises(PermissionError, match="authority mismatch"):
        with capability.borrow_for_scope_factory(
            authority=issue_memory_uow_scope_factory_authority()
        ):
            pass

    capability.close()
    assert capability.active is False
    with pytest.raises(RuntimeError, match="released"):
        capability.issue_canonical_mutation_commit_port(authority=_scope_authority)


def test_transaction_close_refuses_an_active_borrow() -> None:
    _connection, _driver, scope_authority, capability = _capability()
    with capability.borrow_for_scope_factory(authority=scope_authority):
        with pytest.raises(RuntimeError, match="active borrows"):
            capability.close()
