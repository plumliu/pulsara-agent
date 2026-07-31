"""Closed prerequisite state machine for migrations 0005 through 0008."""

from __future__ import annotations

from typing import cast

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.projection_jobs.contracts import (
    PostgresMigrationPreparationKind,
    PostgresMigrationPreparationRequirementFact,
    PostgresMigrationProgressOutcomeFact,
    build_projection_fact,
)


def next_projection_migration_requirement(
    *,
    current_head_version: int,
    target_head_version: int,
    database_target_fingerprint: str,
    current_registry_prefix_fingerprint: str,
    legacy_surface_binding_plan_ready: bool,
    timeline_coverage_ready: bool,
    evidence_coverage_ready: bool,
) -> PostgresMigrationPreparationRequirementFact | None:
    if current_head_version >= target_head_version:
        return None
    if current_head_version == 5 and not legacy_surface_binding_plan_ready:
        return _requirement(
            kind=PostgresMigrationPreparationKind.LEGACY_SURFACE_BINDING_PLAN,
            current_head_version=current_head_version,
            next_migration_version=6,
            database_target_fingerprint=database_target_fingerprint,
            current_registry_prefix_fingerprint=(current_registry_prefix_fingerprint),
            maintenance_operation_kind="legacy_surface_binding_plan",
        )
    if current_head_version == 6 and not timeline_coverage_ready:
        return _requirement(
            kind=(
                PostgresMigrationPreparationKind.RUN_TIMELINE_PRE_ACTIVATION_COVERAGE
            ),
            current_head_version=current_head_version,
            next_migration_version=7,
            database_target_fingerprint=database_target_fingerprint,
            current_registry_prefix_fingerprint=(current_registry_prefix_fingerprint),
            maintenance_operation_kind="run_timeline_pre_activation_drain",
        )
    if current_head_version == 7 and not evidence_coverage_ready:
        return _requirement(
            kind=(
                PostgresMigrationPreparationKind.TOOL_RESULT_EVIDENCE_PRE_ACTIVATION_COVERAGE
            ),
            current_head_version=current_head_version,
            next_migration_version=8,
            database_target_fingerprint=database_target_fingerprint,
            current_registry_prefix_fingerprint=(current_registry_prefix_fingerprint),
            maintenance_operation_kind=("tool_result_evidence_pre_activation_drain"),
        )
    return None


def migration_progress_outcome(
    *,
    initial_head_version: int | None,
    resulting_head_version: int | None,
    target_head_version: int,
    applied_versions: tuple[int, ...],
    requirement: PostgresMigrationPreparationRequirementFact | None,
    resulting_registry_prefix_fingerprint: str,
) -> PostgresMigrationProgressOutcomeFact:
    if requirement is not None:
        status = "preparation_required"
    elif (
        resulting_head_version is not None
        and resulting_head_version >= target_head_version
        and not applied_versions
    ):
        status = "up_to_date"
    else:
        status = "advanced"
    return cast(
        PostgresMigrationProgressOutcomeFact,
        build_projection_fact(
            PostgresMigrationProgressOutcomeFact,
            schema_version="postgres_migration_progress_outcome.v1",
            status=status,
            initial_head_version=initial_head_version,
            resulting_head_version=resulting_head_version,
            applied_versions=applied_versions,
            preparation_requirement=requirement,
            resulting_registry_prefix_fingerprint=(
                resulting_registry_prefix_fingerprint
            ),
        ),
    )


def _requirement(
    *,
    kind: PostgresMigrationPreparationKind,
    current_head_version: int,
    next_migration_version: int,
    database_target_fingerprint: str,
    current_registry_prefix_fingerprint: str,
    maintenance_operation_kind: str,
) -> PostgresMigrationPreparationRequirementFact:
    contract_fingerprint = context_fingerprint(
        "postgres-migration-preparation-contract:v1",
        {
            "preparation_kind": kind.value,
            "current_head_version": current_head_version,
            "next_migration_version": next_migration_version,
            "expected_registry_prefix_fingerprint": (
                current_registry_prefix_fingerprint
            ),
            "required_maintenance_operation_kind": (maintenance_operation_kind),
        },
    )
    return cast(
        PostgresMigrationPreparationRequirementFact,
        build_projection_fact(
            PostgresMigrationPreparationRequirementFact,
            schema_version="postgres_migration_preparation_requirement.v1",
            current_head_version=current_head_version,
            next_migration_version=next_migration_version,
            preparation_kind=kind,
            expected_registry_prefix_fingerprint=(current_registry_prefix_fingerprint),
            expected_database_target_fingerprint=(database_target_fingerprint),
            required_maintenance_operation_kind=(maintenance_operation_kind),
            preparation_contract_fingerprint=contract_fingerprint,
        ),
    )


__all__ = [
    "migration_progress_outcome",
    "next_projection_migration_requirement",
]
