from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from pulsara_agent.ports.projection_jobs import (
    build_projection_migration_transaction_identity,
)
from pulsara_agent.runtime.projection_jobs.migration_port import (
    PostgresProjectionMigrationPreparationPort,
)
from pulsara_agent.runtime.projection_jobs.pre_activation import (
    ProjectionMigrationPreparationReport,
)


class _Capability:
    def __init__(self, identity) -> None:
        self.transaction_identity = identity
        self.active = True
        self.borrowed_authorities = []
        self.connection = object()

    def assert_active(self) -> None:
        if not self.active:
            raise RuntimeError("released")

    @contextmanager
    def borrow_for_port(self, *, authority):
        self.assert_active()
        self.borrowed_authorities.append(authority)
        yield self.connection
        self.assert_active()


class _Coordinator:
    def prepare_legacy_surface_bindings(self, *, deadline_monotonic):
        assert deadline_monotonic == 100.0
        return ProjectionMigrationPreparationReport(
            preparation_kind="legacy_surface_binding_plan.v1",
            target_migration_version=6,
            maintenance_operation_id="maintenance:binding",
            maintenance_epoch_fingerprint="sha256:epoch",
            durable_authority_fingerprint="sha256:authority",
            item_count=3,
        )

    def drain_pre_activation(self, *, kind, deadline_monotonic):
        raise AssertionError((kind, deadline_monotonic))


def _identity():
    return build_projection_migration_transaction_identity(
        database_target_fingerprint="sha256:database",
        database_oid=10,
        backend_pid=22,
        current_head_version=5,
        current_registry_prefix_fingerprint="sha256:registry",
        maintenance_operation_id="maintenance:test",
        maintenance_epoch_fingerprint="sha256:maintenance",
        transaction_generation=1,
    )


def test_migration_readiness_uses_sealed_transaction_and_exact_identity(
    monkeypatch,
) -> None:
    port = PostgresProjectionMigrationPreparationPort(coordinator=_Coordinator())
    capability = _Capability(_identity())
    seen = []

    def _readiness(connection, **kwargs):
        seen.append((connection, kwargs))
        return SimpleNamespace(
            legacy_surface_binding_plan_ready=True,
            timeline_coverage_ready=False,
            evidence_coverage_ready=True,
        )

    monkeypatch.setattr(
        "pulsara_agent.runtime.projection_jobs.migration_port.projection_migration_readiness",
        _readiness,
    )
    result = port.readiness(
        transaction=capability,
        current_head_version=5,
        database_target_fingerprint="sha256:database",
    )

    assert result.legacy_surface_binding_plan_ready is True
    assert result.timeline_coverage_ready is False
    assert seen == [
        (
            capability.connection,
            {
                "current_head_version": 5,
                "database_target_fingerprint": "sha256:database",
            },
        )
    ]
    assert capability.borrowed_authorities == [port.port_authority]
    with pytest.raises(ValueError, match="authority mismatch"):
        port.readiness(
            transaction=capability,
            current_head_version=6,
            database_target_fingerprint="sha256:database",
        )


def test_migration_preparation_report_is_a_bounded_view() -> None:
    port = PostgresProjectionMigrationPreparationPort(coordinator=_Coordinator())

    report = port.prepare_legacy_surface_bindings(deadline_monotonic=100.0)

    assert report.preparation_kind == "legacy_surface_binding_plan.v1"
    assert report.target_migration_version == 6
    assert report.item_count == 3
    assert not hasattr(report, "connection")


def test_migration_port_rejects_released_transaction() -> None:
    port = PostgresProjectionMigrationPreparationPort(coordinator=_Coordinator())
    capability = _Capability(_identity())
    capability.active = False

    with pytest.raises(RuntimeError, match="released"):
        port.readiness(
            transaction=capability,
            current_head_version=5,
            database_target_fingerprint="sha256:database",
        )
