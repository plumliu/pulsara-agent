"""Runtime implementation of the sealed projection-migration port."""

from __future__ import annotations

from dataclasses import dataclass, field

from pulsara_agent.ports.projection_jobs import (
    ProjectionMigrationPortAuthority,
    ProjectionMigrationPreparationReportView,
    ProjectionMigrationReadinessView,
    ProjectionMigrationTransactionCapability,
    build_projection_migration_readiness_view,
    issue_projection_migration_port_authority,
)
from pulsara_agent.projection_jobs.contracts import (
    DurableProjectionKind,
    RuntimeWriteAdmissionEpochFact,
)
from pulsara_agent.runtime.projection_jobs.migration_transform import (
    apply_projection_migration_transform,
    protected_relation_resource_for_version,
)
from pulsara_agent.runtime.projection_jobs.pre_activation import (
    PostgresProjectionMigrationPreparationCoordinator,
    ProjectionMigrationPreparationReport,
    projection_migration_readiness,
)


@dataclass(slots=True)
class PostgresProjectionMigrationPreparationPort:
    coordinator: PostgresProjectionMigrationPreparationCoordinator
    _port_authority: ProjectionMigrationPortAuthority = field(init=False)

    def __post_init__(self) -> None:
        self._port_authority = issue_projection_migration_port_authority()

    @property
    def port_authority(self) -> ProjectionMigrationPortAuthority:
        return self._port_authority

    def readiness(
        self,
        *,
        transaction: ProjectionMigrationTransactionCapability,
        current_head_version: int,
        database_target_fingerprint: str,
    ) -> ProjectionMigrationReadinessView:
        transaction.assert_active()
        identity = transaction.transaction_identity
        if (
            identity.current_head_version != current_head_version
            or identity.database_target_fingerprint != database_target_fingerprint
        ):
            raise ValueError("projection migration readiness authority mismatch")
        with transaction.borrow_for_port(authority=self._port_authority) as connection:
            value = projection_migration_readiness(
                connection,
                current_head_version=current_head_version,
                database_target_fingerprint=database_target_fingerprint,
            )
        return build_projection_migration_readiness_view(
            legacy_surface_binding_plan_ready=(value.legacy_surface_binding_plan_ready),
            timeline_coverage_ready=value.timeline_coverage_ready,
            evidence_coverage_ready=value.evidence_coverage_ready,
        )

    def apply_transform(
        self,
        *,
        transaction: ProjectionMigrationTransactionCapability,
        version: int,
        maintenance_epoch: RuntimeWriteAdmissionEpochFact,
        resulting_registry_prefix_fingerprint: str,
    ) -> None:
        transaction.assert_active()
        identity = transaction.transaction_identity
        if (
            identity.maintenance_operation_id
            != maintenance_epoch.maintenance_operation_id
            or identity.maintenance_epoch_fingerprint
            != maintenance_epoch.epoch_fingerprint
        ):
            raise ValueError("projection migration transform authority mismatch")
        with transaction.borrow_for_port(authority=self._port_authority) as connection:
            apply_projection_migration_transform(
                connection,
                version=version,
                maintenance_epoch=maintenance_epoch,
                resulting_registry_prefix_fingerprint=(
                    resulting_registry_prefix_fingerprint
                ),
            )

    def protected_relation_resource_for_version(self, version: int) -> str:
        return protected_relation_resource_for_version(version)

    def prepare_legacy_surface_bindings(
        self, *, deadline_monotonic: float
    ) -> ProjectionMigrationPreparationReportView:
        return _report_view(
            self.coordinator.prepare_legacy_surface_bindings(
                deadline_monotonic=deadline_monotonic
            )
        )

    def drain_pre_activation(
        self,
        *,
        kind: DurableProjectionKind,
        deadline_monotonic: float,
    ) -> ProjectionMigrationPreparationReportView:
        return _report_view(
            self.coordinator.drain_pre_activation(
                kind=kind,
                deadline_monotonic=deadline_monotonic,
            )
        )


def build_postgres_projection_migration_preparation_port(
    *, admin_dsn: str, runtime_dsn: str
) -> PostgresProjectionMigrationPreparationPort:
    return PostgresProjectionMigrationPreparationPort(
        coordinator=PostgresProjectionMigrationPreparationCoordinator(
            admin_dsn=admin_dsn,
            runtime_dsn=runtime_dsn,
        )
    )


def _report_view(
    report: ProjectionMigrationPreparationReport,
) -> ProjectionMigrationPreparationReportView:
    return ProjectionMigrationPreparationReportView(
        preparation_kind=report.preparation_kind,  # type: ignore[arg-type]
        target_migration_version=report.target_migration_version,
        maintenance_operation_id=report.maintenance_operation_id,
        maintenance_epoch_fingerprint=report.maintenance_epoch_fingerprint,
        durable_authority_fingerprint=report.durable_authority_fingerprint,
        item_count=report.item_count,
    )


__all__ = [
    "PostgresProjectionMigrationPreparationPort",
    "build_postgres_projection_migration_preparation_port",
]
