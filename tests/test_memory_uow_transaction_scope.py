from __future__ import annotations

from contextlib import contextmanager
from time import monotonic
from typing import cast

import pytest

from pulsara_agent.event import EventContext
from pulsara_agent.memory.canonical.postgres_uow_scope import (
    PostgresMemoryUowTransactionScopeFactory,
)
from pulsara_agent.memory.canonical.uow_contracts import (
    MemoryUowScopeLeaseReleasedError,
    build_memory_uow_scope_request,
)
from pulsara_agent.memory.canonical.write_gate import (
    MEMORY_WRITE_GATE_CONTRACT_FINGERPRINT,
    MemoryWriteGate,
)
from pulsara_agent.ports.projection_jobs import (
    build_canonical_mutation_transaction_identity,
    issue_memory_uow_scope_factory_authority,
)
from pulsara_agent.projection_jobs.canonical_mutation import build_surface_plan
from pulsara_agent.projection_jobs.contracts import (
    RuntimeSessionBootstrapStateFact,
    RuntimeSessionOwnerSemanticFact,
    build_projection_fact,
)
from pulsara_agent.primitives.context import context_fingerprint


class _Info:
    backend_pid = 91


class _Connection:
    closed = False
    info = _Info()


class _CommitPort:
    def append_bundle(self, *, bundle):
        raise AssertionError(f"unexpected mutation append: {bundle!r}")


class _PhysicalTransaction:
    def __init__(self, *, connection, identity, scope_authority) -> None:
        self.connection = connection
        self.transaction_identity = identity
        self.scope_authority = scope_authority

    @contextmanager
    def borrow_for_scope_factory(self, *, authority):
        if authority is not self.scope_authority:
            raise PermissionError("scope authority mismatch")
        yield self.connection

    def issue_canonical_mutation_commit_port(self, *, authority):
        if authority is not self.scope_authority:
            raise PermissionError("scope authority mismatch")
        return _CommitPort()


class _Provider:
    def __init__(self, transaction) -> None:
        self.transaction = transaction
        self.exited = False

    @contextmanager
    def memory_uow_physical_transaction(self, **_kwargs):
        try:
            yield self.transaction
        finally:
            self.exited = True


class _Driver:
    driver_authority = object()


def _bootstrap_state() -> RuntimeSessionBootstrapStateFact:
    owner = cast(
        RuntimeSessionOwnerSemanticFact,
        build_projection_fact(
            RuntimeSessionOwnerSemanticFact,
            schema_version="runtime_session_owner_semantic.v1",
            runtime_session_id="runtime:test",
            workspace_root="/workspace",
        ),
    )
    return cast(
        RuntimeSessionBootstrapStateFact,
        build_projection_fact(
            RuntimeSessionBootstrapStateFact,
            schema_version="runtime_session_bootstrap_state.v1",
            session_owner=owner,
            ordered_active_cutover_fingerprints=(),
            ordered_pre_activation_cutover_fingerprints=(),
            cutover_set_accumulator=context_fingerprint(
                "runtime-session-bootstrap-cutover-set:v1",
                {"active": (), "pre_activation": ()},
            ),
            background_budget_account_fingerprint="sha256:background-budget-account",
            admission_epoch_fingerprint="sha256:epoch",
        ),
    )


def test_all_six_uow_facades_share_one_lease_and_fail_after_scope_exit() -> None:
    scope_authority = issue_memory_uow_scope_factory_authority()
    connection = _Connection()
    identity = build_canonical_mutation_transaction_identity(
        schema_binding_fingerprint="sha256:schema",
        connection_provider_borrower_id="borrower:test",
        transaction_owner_id="uow:test",
        transaction_generation=1,
        backend_pid=connection.info.backend_pid,
        admission_epoch_fingerprint="sha256:epoch",
        admission_guard_lock_identity_fingerprint="sha256:guard",
    )
    provider = _Provider(
        _PhysicalTransaction(
            connection=connection,
            identity=identity,
            scope_authority=scope_authority,
        )
    )
    factory = PostgresMemoryUowTransactionScopeFactory(
        connection_provider=cast(object, provider),
        mutation_driver=cast(object, _Driver()),
        scope_factory_authority=scope_authority,
        gate=MemoryWriteGate(),
        memory_write_gate_contract_fingerprint=(MEMORY_WRITE_GATE_CONTRACT_FINGERPRINT),
    )
    request = build_memory_uow_scope_request(
        runtime_session_id="runtime:test",
        workspace_root="/workspace",
        graph_id="graph:test",
        session_bootstrap_state=_bootstrap_state(),
        transaction_owner_id="uow:test",
        transaction_generation=1,
        surface_plan=build_surface_plan(()),
        memory_write_gate_contract_fingerprint=(MEMORY_WRITE_GATE_CONTRACT_FINGERPRINT),
        deadline_monotonic=monotonic() + 30.0,
    )

    with factory.open_scope(request=request) as scope:
        facades = scope.repositories
        fingerprints = {
            item.facade_identity.scope_lease_identity_fingerprint
            for item in (
                facades.graph,
                facades.decisions,
                facades.outbox,
                facades.runtime_events,
                facades.lifecycle,
                facades.memory_write_service,
            )
        }
        assert fingerprints == {scope.scope_lease_identity.identity_fingerprint}
        retained = facades

    assert provider.exited is True
    operations = (
        lambda: retained.graph.has_jsonld("node:test"),
        lambda: retained.decisions.append_candidate(cast(object, None)),
        lambda: retained.outbox.append_decision(
            cast(object, None), graph_id="graph:test", requested_surfaces=()
        ),
        lambda: retained.runtime_events.append_batch(
            (), governance_batch_id="batch:test", decision_id="decision:test"
        ),
        lambda: retained.lifecycle.mark_stale(
            node_id="node:test", governance_batch_id="batch:test"
        ),
        lambda: retained.memory_write_service.submit(
            {},
            event_context=EventContext(
                run_id="run:test", turn_id="turn:test", reply_id="reply:test"
            ),
        ),
    )
    for operation in operations:
        with pytest.raises(MemoryUowScopeLeaseReleasedError):
            operation()
