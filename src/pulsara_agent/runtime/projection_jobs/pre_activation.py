"""Privileged preparation for staged projection migrations.

The schema migrator deliberately does not manufacture its own prerequisites.
This module owns the two durable preparation families used by migrations
0006-0008: legacy surface binding plans and pre-activation coverage receipts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from importlib.resources import files
from typing import Any, Iterable, cast

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.event_log.transcript_prefix import (
    EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
    EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR,
)
from pulsara_agent.runtime.projection_jobs.legacy_mutation_payload import (
    parse_legacy_mutation_payload,
)
from pulsara_agent.primitives._context_base import (
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.runtime.projection_jobs.contracts import (
    CanonicalMutationPlannedSurfaceFact,
    CanonicalMutationSurface,
    DurableProjectionKind,
    DurableProjectionKindActivationSemanticFact,
    DurableProjectionLedgerHorizonFact,
    LegacySurfaceHistoricalBindingProofFact,
    LegacySurfaceMigrationBindingEntryFact,
    LegacySurfaceMigrationBindingPageFact,
    LegacySurfaceMigrationBindingPlanFact,
    LegacySurfaceMigrationRebindAuthorityFact,
    PreActivationProjectionCoverageReceiptFact,
    PreActivationProjectionHookContractFact,
    PreActivationProjectionHookContractSemanticFact,
    PreActivationProjectionSessionCutoverFact,
    RuntimeWriteAdmissionMode,
    RuntimeWriteMaintenanceAuthorityFact,
    build_projection_fact,
)
from pulsara_agent.runtime.projection_jobs.mutation_writer import (
    build_surface_handler_contract,
)
from pulsara_agent.runtime.projection_jobs.registry import (
    default_projection_delivery_policy,
)
from pulsara_agent.storage.migrations.registry import (
    POSTGRES_MIGRATION_REGISTRY,
    PostgresMigrationRegistry,
)
from pulsara_agent.storage.migrations.runner import (
    PostgresAdminConnectionFactory,
    PostgresDatabaseIdentity,
    _acquire_advisory_lock,
    _apply_local_deadline,
    _read_identity_from_connection,
    _validate_transaction_domain,
)
from pulsara_agent.storage.postgres_endpoint import (
    ResolvedPostgresConnectionFactory,
)
from pulsara_agent.storage.runtime_write_admission import (
    acquire_maintenance_runtime_write_guard,
    enter_runtime_write_maintenance,
    read_runtime_write_epoch,
)


_LEGACY_PLAN_RESOURCE = "0006_legacy_surface_binding_plan_contract_v1.json"
_PRE_ACTIVATION_RESOURCE = "0006_pre_activation_projection_contracts_v1.json"
_ACTIVATION_RESOURCE_BY_KIND = {
    DurableProjectionKind.RUN_TIMELINE: "0007_run_timeline_activation_v1.json",
    DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE: (
        "0008_tool_result_evidence_activation_v1.json"
    ),
}
_TARGET_VERSION_BY_KIND = {
    DurableProjectionKind.RUN_TIMELINE: 7,
    DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE: 8,
}
_MAX_PAGE_ROWS = 256
_MAX_PAGE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ProjectionMigrationPreparationReport:
    preparation_kind: str
    target_migration_version: int
    maintenance_operation_id: str
    maintenance_epoch_fingerprint: str
    durable_authority_fingerprint: str
    item_count: int


@dataclass(frozen=True, slots=True)
class ProjectionMigrationReadiness:
    legacy_surface_binding_plan_ready: bool
    timeline_coverage_ready: bool
    evidence_coverage_ready: bool


def load_pre_activation_contract_semantics(
) -> tuple[PreActivationProjectionHookContractSemanticFact, ...]:
    payload = _resource_json(_PRE_ACTIVATION_RESOURCE)
    semantics = tuple(
        PreActivationProjectionHookContractSemanticFact.model_validate(item)
        for item in payload["ordered_contract_semantics"]
    )
    if tuple(item.projection_kind for item in semantics) != tuple(
        DurableProjectionKind
    ):
        raise ValueError("packaged pre-activation contract registry is incomplete")
    return semantics


def load_activation_semantic(
    kind: DurableProjectionKind,
) -> DurableProjectionKindActivationSemanticFact:
    payload = _resource_json(_ACTIVATION_RESOURCE_BY_KIND[kind])
    semantic = DurableProjectionKindActivationSemanticFact.model_validate(
        payload["activation_semantic"]
    )
    if semantic.projection_kind is not kind:
        raise ValueError("packaged activation kind drifted")
    return semantic


def projection_migration_readiness(
    connection: Connection,
    *,
    current_head_version: int,
    database_target_fingerprint: str,
) -> ProjectionMigrationReadiness:
    """Read prerequisites without mutating or entering maintenance."""

    if current_head_version < 5:
        return ProjectionMigrationReadiness(False, False, False)
    epoch = read_runtime_write_epoch(connection, privileged=True)
    if epoch.database_target_fingerprint != database_target_fingerprint:
        raise ValueError("runtime write epoch/database target mismatch")
    legacy_ready = (
        current_head_version >= 6
        or (
            current_head_version == 5
            and epoch.mode is RuntimeWriteAdmissionMode.MAINTENANCE
            and epoch.target_migration_version == 6
            and _exact_legacy_binding_plan(
                connection,
                epoch_fingerprint=epoch.epoch_fingerprint,
                maintenance_operation_id=epoch.maintenance_operation_id,
                database_target_fingerprint=database_target_fingerprint,
                expected_v5_prefix=POSTGRES_MIGRATION_REGISTRY.definition(
                    5
                ).registry_prefix_fingerprint,
            )
            is not None
        )
    )
    timeline_ready = (
        current_head_version >= 7
        or (
            current_head_version == 6
            and epoch.mode is RuntimeWriteAdmissionMode.MAINTENANCE
            and epoch.target_migration_version == 7
            and _coverage_set_is_complete(
                connection,
                kind=DurableProjectionKind.RUN_TIMELINE,
                maintenance_operation_id=epoch.maintenance_operation_id,
                maintenance_epoch_fingerprint=epoch.epoch_fingerprint,
            )
        )
    )
    evidence_ready = (
        current_head_version >= 8
        or (
            current_head_version == 7
            and epoch.mode is RuntimeWriteAdmissionMode.MAINTENANCE
            and epoch.target_migration_version == 8
            and _coverage_set_is_complete(
                connection,
                kind=DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE,
                maintenance_operation_id=epoch.maintenance_operation_id,
                maintenance_epoch_fingerprint=epoch.epoch_fingerprint,
            )
        )
    )
    return ProjectionMigrationReadiness(
        legacy_surface_binding_plan_ready=legacy_ready,
        timeline_coverage_ready=timeline_ready,
        evidence_coverage_ready=evidence_ready,
    )


class PostgresProjectionMigrationPreparationCoordinator:
    """The only privileged owner of DPJ migration prerequisites."""

    def __init__(
        self,
        *,
        admin_dsn: str,
        runtime_dsn: str,
        registry: PostgresMigrationRegistry = POSTGRES_MIGRATION_REGISTRY,
    ) -> None:
        self._admin_factory = PostgresAdminConnectionFactory(admin_dsn)
        self._runtime_factory = ResolvedPostgresConnectionFactory(
            runtime_dsn,
            application_name="pulsara-projection-migration-runtime-probe",
        )
        if (
            self._admin_factory.endpoint.endpoint_fingerprint
            != self._runtime_factory.endpoint.endpoint_fingerprint
        ):
            raise ValueError(
                "admin and runtime DSNs must resolve to the same database target"
            )
        self._registry = registry

    def prepare_legacy_surface_bindings(
        self,
        *,
        deadline_monotonic: float,
    ) -> ProjectionMigrationPreparationReport:
        connection, identity, authority = self._open_maintenance(
            target_migration_version=6,
            operation_kind="legacy-surface-binding-plan",
            deadline_monotonic=deadline_monotonic,
        )
        try:
            with connection.transaction():
                _apply_local_deadline(connection, deadline_monotonic)
                epoch = read_runtime_write_epoch(connection, privileged=True)
                guard = acquire_maintenance_runtime_write_guard(
                    connection,
                    expected_epoch=epoch,
                    transaction_owner_id=(
                        "legacy-surface-binding-plan:"
                        + epoch.maintenance_operation_id
                    ),
                )
                plan = _exact_legacy_binding_plan(
                    connection,
                    epoch_fingerprint=epoch.epoch_fingerprint,
                    maintenance_operation_id=epoch.maintenance_operation_id,
                    database_target_fingerprint=(
                        self._admin_factory.endpoint.endpoint_fingerprint
                    ),
                    expected_v5_prefix=self._registry.definition(
                        5
                    ).registry_prefix_fingerprint,
                )
                if plan is None:
                    plan = _write_legacy_binding_plan(
                        connection,
                        database_target_fingerprint=(
                            self._admin_factory.endpoint.endpoint_fingerprint
                        ),
                        expected_v5_prefix=self._registry.definition(
                            5
                        ).registry_prefix_fingerprint,
                        maintenance_operation_id=(
                            epoch.maintenance_operation_id
                        ),
                        maintenance_authority_fingerprint=(
                            guard.maintenance_authority_fingerprint
                        ),
                    )
            return ProjectionMigrationPreparationReport(
                preparation_kind="legacy_surface_binding_plan.v1",
                target_migration_version=6,
                maintenance_operation_id=cast(
                    str, epoch.maintenance_operation_id
                ),
                maintenance_epoch_fingerprint=epoch.epoch_fingerprint,
                durable_authority_fingerprint=plan.plan_fingerprint,
                item_count=plan.binding_entry_count,
            )
        finally:
            connection.close()

    def drain_pre_activation(
        self,
        *,
        kind: DurableProjectionKind,
        deadline_monotonic: float,
    ) -> ProjectionMigrationPreparationReport:
        target_version = _TARGET_VERSION_BY_KIND[kind]
        connection, _identity, _authority = self._open_maintenance(
            target_migration_version=target_version,
            operation_kind=f"pre-activation-drain:{kind.value}",
            deadline_monotonic=deadline_monotonic,
        )
        try:
            from pulsara_agent.runtime.projection_jobs.projection_handlers import (
                drain_pre_activation_kind,
            )

            receipts = drain_pre_activation_kind(
                connection,
                kind=kind,
                database_target_fingerprint=(
                    self._admin_factory.endpoint.endpoint_fingerprint
                ),
                deadline_monotonic=deadline_monotonic,
            )
            epoch = read_runtime_write_epoch(connection, privileged=True)
            accumulator = context_fingerprint(
                "pre-activation-coverage-receipt-set:v1",
                tuple(item.receipt_fingerprint for item in receipts),
            )
            return ProjectionMigrationPreparationReport(
                preparation_kind=(
                    "run_timeline_pre_activation_coverage.v1"
                    if kind is DurableProjectionKind.RUN_TIMELINE
                    else "tool_result_evidence_pre_activation_coverage.v1"
                ),
                target_migration_version=target_version,
                maintenance_operation_id=cast(
                    str, epoch.maintenance_operation_id
                ),
                maintenance_epoch_fingerprint=epoch.epoch_fingerprint,
                durable_authority_fingerprint=accumulator,
                item_count=len(receipts),
            )
        finally:
            connection.close()

    def _open_maintenance(
        self,
        *,
        target_migration_version: int,
        operation_kind: str,
        deadline_monotonic: float,
    ) -> tuple[
        Connection,
        PostgresDatabaseIdentity,
        RuntimeWriteMaintenanceAuthorityFact | None,
    ]:
        runtime_connection = self._runtime_factory.connect(
            deadline_monotonic=deadline_monotonic,
            autocommit=True,
        )
        try:
            runtime_identity = _read_identity_from_connection(
                runtime_connection
            )
        finally:
            runtime_connection.close()
        connection = self._admin_factory.connect(
            deadline_monotonic=deadline_monotonic
        )
        try:
            admin_identity = _read_identity_from_connection(connection)
            _validate_transaction_domain(admin_identity, runtime_identity)
            _acquire_advisory_lock(
                connection,
                database_oid=admin_identity.database_oid,
                shared=False,
                deadline_monotonic=deadline_monotonic,
            )
            rows = connection.execute(
                """
                SELECT version, registry_prefix_fingerprint
                FROM public.pulsara_schema_migrations
                ORDER BY version
                """
            ).fetchall()
            if not rows:
                raise ValueError("projection preparation requires a migrated database")
            current_version = int(rows[-1][0])
            if current_version != target_migration_version - 1:
                raise ValueError(
                    "projection preparation does not match the current migration head"
                )
            expected_prefix = self._registry.definition(
                current_version
            ).registry_prefix_fingerprint
            if str(rows[-1][1]) != expected_prefix:
                raise ValueError("migration registry prefix drifted")
            epoch = read_runtime_write_epoch(connection, privileged=True)
            if (
                epoch.mode is RuntimeWriteAdmissionMode.MAINTENANCE
                and epoch.target_migration_version == target_migration_version
            ):
                return connection, runtime_identity, None
            if epoch.mode is not RuntimeWriteAdmissionMode.NORMAL:
                raise ValueError(
                    "database is already in a different maintenance operation"
                )
            operation_id = "projection-maintenance:" + context_fingerprint(
                "projection-maintenance-operation-id:v1",
                {
                    "operation_kind": operation_kind,
                    "target_migration_version": target_migration_version,
                    "database_target_fingerprint": (
                        self._admin_factory.endpoint.endpoint_fingerprint
                    ),
                    "normal_epoch_fingerprint": epoch.epoch_fingerprint,
                    "registry_prefix_fingerprint": expected_prefix,
                },
            )
            with connection.transaction():
                _apply_local_deadline(connection, deadline_monotonic)
                authority = enter_runtime_write_maintenance(
                    connection,
                    current_epoch=epoch,
                    maintenance_operation_id=operation_id,
                    target_migration_version=target_migration_version,
                )
            return connection, runtime_identity, authority
        except BaseException:
            connection.close()
            raise


def packaged_pre_activation_contracts(
    *,
    resulting_v6_registry_prefix_fingerprint: str,
) -> tuple[PreActivationProjectionHookContractFact, ...]:
    return tuple(
        cast(
            PreActivationProjectionHookContractFact,
            build_projection_fact(
                PreActivationProjectionHookContractFact,
                schema_version="pre_activation_projection_hook_contract.v1",
                contract_semantic=semantic,
                installation_migration_version=6,
                resulting_migration_registry_prefix_fingerprint=(
                    resulting_v6_registry_prefix_fingerprint
                ),
            ),
        )
        for semantic in load_pre_activation_contract_semantics()
    )


def ledger_horizon_for_session(
    connection: Connection,
    runtime_session_id: str,
) -> DurableProjectionLedgerHorizonFact:
    row = connection.execute(
        """
        SELECT sequence, ledger_continuity_accumulator,
               ledger_payload_prefix_bytes,
               transcript_semantic_prefix_count,
               transcript_semantic_prefix_accumulator
        FROM public.agent_events
        WHERE session_id = %s
        ORDER BY sequence DESC
        LIMIT 1
        """,
        (runtime_session_id,),
    ).fetchone()
    if row is None:
        values = (
            0,
            EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
            0,
            0,
            EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR,
        )
    else:
        values = row
    return cast(
        DurableProjectionLedgerHorizonFact,
        build_projection_fact(
            DurableProjectionLedgerHorizonFact,
            schema_version="durable_projection_ledger_horizon.v1",
            runtime_session_id=runtime_session_id,
            through_sequence=int(values[0]),
            ledger_continuity_accumulator=str(values[1]),
            ledger_payload_prefix_bytes=int(values[2]),
            transcript_semantic_prefix_count=int(values[3]),
            transcript_semantic_prefix_accumulator=str(values[4]),
        ),
    )


def build_pre_activation_cutover(
    *,
    runtime_session_id: str,
    contract: PreActivationProjectionHookContractFact,
    horizon: DurableProjectionLedgerHorizonFact,
) -> PreActivationProjectionSessionCutoverFact:
    if horizon.runtime_session_id != runtime_session_id:
        raise ValueError("pre-activation cutover horizon/session mismatch")
    return cast(
        PreActivationProjectionSessionCutoverFact,
        build_projection_fact(
            PreActivationProjectionSessionCutoverFact,
            schema_version="pre_activation_projection_session_cutover.v1",
            runtime_session_id=runtime_session_id,
            projection_kind=contract.contract_semantic.projection_kind,
            pre_activation_contract_fingerprint=contract.contract_fingerprint,
            cutover_through_sequence=horizon.through_sequence,
            cutover_ledger_continuity_accumulator=(
                horizon.ledger_continuity_accumulator
            ),
            cutover_ledger_payload_prefix_bytes=(
                horizon.ledger_payload_prefix_bytes
            ),
            cutover_transcript_semantic_prefix_count=(
                horizon.transcript_semantic_prefix_count
            ),
            cutover_transcript_semantic_prefix_accumulator=(
                horizon.transcript_semantic_prefix_accumulator
            ),
            migration_version=contract.installation_migration_version,
            migration_registry_prefix_fingerprint=(
                contract.resulting_migration_registry_prefix_fingerprint
            ),
        ),
    )


def _write_legacy_binding_plan(
    connection: Connection,
    *,
    database_target_fingerprint: str,
    expected_v5_prefix: str,
    maintenance_operation_id: str | None,
    maintenance_authority_fingerprint: str | None,
) -> LegacySurfaceMigrationBindingPlanFact:
    if not maintenance_operation_id or not maintenance_authority_fingerprint:
        raise ValueError("legacy binding plan requires maintenance authority")
    _validate_legacy_plan_resource()
    rows = tuple(
        connection.cursor(row_factory=dict_row).execute(
            """
            SELECT outbox_id, graph_id, governance_batch_id, decision_id,
                   mutation_lane, sequence_key, target_entry_key,
                   dirty_memory_ids, payload, status, attempt_count,
                   created_at, applied_at
            FROM public.memory_write_outbox
            ORDER BY sequence_key, created_at, outbox_id
            """
        ).fetchall()
    )
    row_fingerprints = tuple(_legacy_row_fingerprint(row) for row in rows)
    legacy_root = context_fingerprint(
        "legacy-canonical-mutation-row-accumulator:v1",
        row_fingerprints,
    )
    entries: list[LegacySurfaceMigrationBindingEntryFact] = []
    privileged: list[str] = []
    for row in rows:
        payload = parse_legacy_mutation_payload(row["payload"])
        payload_sha = _payload_sha256(row["payload"])
        for legacy_surface, status in sorted(
            payload.surface_apply_status.items()
        ):
            surface = _surface_from_legacy(legacy_surface)
            planned = cast(
                CanonicalMutationPlannedSurfaceFact,
                build_projection_fact(
                    CanonicalMutationPlannedSurfaceFact,
                    schema_version="canonical_mutation_planned_surface.v1",
                    handler_contract=build_surface_handler_contract(surface),
                    delivery_policy=default_projection_delivery_policy(),
                ),
            )
            if status == "applied":
                target_proof = _legacy_applied_target_proof(
                    connection,
                    row=row,
                    surface=surface,
                    payload_sha=payload_sha,
                )
                authority = cast(
                    LegacySurfaceHistoricalBindingProofFact,
                    build_projection_fact(
                        LegacySurfaceHistoricalBindingProofFact,
                        schema_version=(
                            "legacy_surface_historical_binding_proof.v1"
                        ),
                        binding_kind="historical_confirmed",
                        surface=surface,
                        historical_handler_contract=(
                            planned.handler_contract
                        ),
                        observed_target_semantic_identity=target_proof,
                        observed_target_contract_fingerprint=(
                            planned.handler_contract
                            .target_compatibility_fingerprint
                        ),
                        ordered_target_authority_fingerprints=(
                            target_proof,
                        ),
                    ),
                )
            elif status in {"pending", "failed"}:
                no_side_effect = context_fingerprint(
                    "legacy-surface-no-full-side-effect-proof:v1",
                    {
                        "legacy_outbox_id": str(row["outbox_id"]),
                        "surface": surface.value,
                        "legacy_status": status,
                        "payload_sha256": payload_sha,
                        "claim_token_present": bool(
                            row.get("vector_claim_token")
                        ),
                    },
                )
                authority = cast(
                    LegacySurfaceMigrationRebindAuthorityFact,
                    build_projection_fact(
                        LegacySurfaceMigrationRebindAuthorityFact,
                        schema_version=(
                            "legacy_surface_migration_rebind_authority.v1"
                        ),
                        binding_kind="migration_rebound",
                        authority_id=(
                            "legacy-surface-rebind:"
                            + context_fingerprint(
                                "legacy-surface-rebind-id:v1",
                                {
                                    "outbox_id": str(row["outbox_id"]),
                                    "surface": surface.value,
                                    "maintenance_operation_id": (
                                        maintenance_operation_id
                                    ),
                                },
                            )
                        ),
                        database_target_fingerprint=(
                            database_target_fingerprint
                        ),
                        maintenance_authority_fingerprint=(
                            maintenance_authority_fingerprint
                        ),
                        legacy_outbox_id=str(row["outbox_id"]),
                        surface=surface,
                        expected_legacy_status=status,
                        no_full_side_effect_proof_fingerprint=no_side_effect,
                        resulting_planned_surface=planned,
                    ),
                )
                privileged.append(authority.authority_fingerprint)
            else:
                raise ValueError(
                    f"unsupported legacy surface status {status!r}"
                )
            entries.append(
                cast(
                    LegacySurfaceMigrationBindingEntryFact,
                    build_projection_fact(
                        LegacySurfaceMigrationBindingEntryFact,
                        schema_version=(
                            "legacy_surface_migration_binding_entry.v1"
                        ),
                        legacy_outbox_id=str(row["outbox_id"]),
                        legacy_payload_sha256=payload_sha,
                        surface=surface,
                        legacy_surface_status=status,
                        binding_authority=authority,
                    ),
                )
            )
    pages: list[LegacySurfaceMigrationBindingPageFact] = []
    previous: str | None = None
    for page_index, chunk in enumerate(_bounded_chunks(entries)):
        entry_accumulator = context_fingerprint(
            "legacy-surface-binding-page-entry-accumulator:v1",
            tuple(item.entry_fingerprint for item in chunk),
        )
        page = cast(
            LegacySurfaceMigrationBindingPageFact,
            build_projection_fact(
                LegacySurfaceMigrationBindingPageFact,
                schema_version="legacy_surface_migration_binding_page.v1",
                page_index=page_index,
                previous_page_fingerprint=previous,
                ordered_entries=chunk,
                entry_count=len(chunk),
                entry_accumulator=entry_accumulator,
                canonical_utf8_bytes=len(
                    canonical_json_bytes(
                        tuple(item.model_dump(mode="json") for item in chunk)
                    )
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO public.canonical_mutation_v2_migration_binding_plan_pages (
                page_fingerprint, maintenance_operation_id, page_index,
                page_payload
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (page_fingerprint) DO NOTHING
            """,
            (
                page.page_fingerprint,
                maintenance_operation_id,
                page.page_index,
                Jsonb(page.model_dump(mode="json")),
            ),
        )
        pages.append(page)
        previous = page.page_fingerprint
    page_accumulator = context_fingerprint(
        "legacy-surface-binding-page-accumulator:v1",
        tuple(item.page_fingerprint for item in pages),
    )
    entry_accumulator = context_fingerprint(
        "legacy-surface-binding-entry-accumulator:v1",
        tuple(item.entry_fingerprint for item in entries),
    )
    plan_id = "legacy-surface-binding-plan:" + context_fingerprint(
        "legacy-surface-binding-plan-id:v1",
        {
            "database_target_fingerprint": database_target_fingerprint,
            "expected_v5_registry_prefix_fingerprint": expected_v5_prefix,
            "maintenance_authority_fingerprint": (
                maintenance_authority_fingerprint
            ),
            "legacy_row_accumulator": legacy_root,
        },
    )
    plan = cast(
        LegacySurfaceMigrationBindingPlanFact,
        build_projection_fact(
            LegacySurfaceMigrationBindingPlanFact,
            schema_version="legacy_surface_migration_binding_plan.v1",
            plan_id=plan_id,
            database_target_fingerprint=database_target_fingerprint,
            expected_v5_registry_prefix_fingerprint=expected_v5_prefix,
            maintenance_authority_fingerprint=(
                maintenance_authority_fingerprint
            ),
            legacy_row_count=len(rows),
            legacy_row_accumulator=legacy_root,
            binding_page_count=len(pages),
            ordered_binding_page_fingerprint_accumulator=page_accumulator,
            binding_entry_count=len(entries),
            binding_entry_accumulator=entry_accumulator,
            ordered_privileged_authority_fingerprints=tuple(privileged),
        ),
    )
    connection.execute(
        """
        INSERT INTO public.canonical_mutation_v2_migration_binding_plans (
            plan_id, maintenance_operation_id, plan_payload, plan_fingerprint
        ) VALUES (%s, %s, %s, %s)
        ON CONFLICT (plan_id) DO NOTHING
        """,
        (
            plan.plan_id,
            maintenance_operation_id,
            Jsonb(plan.model_dump(mode="json")),
            plan.plan_fingerprint,
        ),
    )
    observed = connection.execute(
        """
        SELECT plan_payload, plan_fingerprint
        FROM public.canonical_mutation_v2_migration_binding_plans
        WHERE plan_id = %s
        """,
        (plan.plan_id,),
    ).fetchone()
    if (
        observed is None
        or LegacySurfaceMigrationBindingPlanFact.model_validate(
            observed[0]
        )
        != plan
        or str(observed[1]) != plan.plan_fingerprint
    ):
        raise ValueError("legacy surface binding plan exact confirmation failed")
    return plan


def _exact_legacy_binding_plan(
    connection: Connection,
    *,
    epoch_fingerprint: str,
    maintenance_operation_id: str | None,
    database_target_fingerprint: str,
    expected_v5_prefix: str,
) -> LegacySurfaceMigrationBindingPlanFact | None:
    del epoch_fingerprint
    if not maintenance_operation_id:
        return None
    rows = tuple(
        connection.execute(
            """
            SELECT plan_payload, plan_fingerprint
            FROM public.canonical_mutation_v2_migration_binding_plans
            WHERE maintenance_operation_id = %s
            """,
            (maintenance_operation_id,),
        ).fetchall()
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("maintenance operation has multiple binding plans")
    plan = LegacySurfaceMigrationBindingPlanFact.model_validate(rows[0][0])
    if (
        plan.plan_fingerprint != str(rows[0][1])
        or plan.database_target_fingerprint != database_target_fingerprint
        or plan.expected_v5_registry_prefix_fingerprint != expected_v5_prefix
    ):
        raise ValueError("legacy binding plan authority drifted")
    page_rows = tuple(
        connection.execute(
            """
            SELECT page_payload, page_fingerprint
            FROM public.canonical_mutation_v2_migration_binding_plan_pages
            WHERE maintenance_operation_id = %s
            ORDER BY page_index
            """,
            (maintenance_operation_id,),
        ).fetchall()
    )
    pages = tuple(
        LegacySurfaceMigrationBindingPageFact.model_validate(row[0])
        for row in page_rows
    )
    if (
        len(pages) != plan.binding_page_count
        or any(
            page.page_fingerprint != str(row[1])
            for page, row in zip(pages, page_rows, strict=True)
        )
        or context_fingerprint(
            "legacy-surface-binding-page-accumulator:v1",
            tuple(item.page_fingerprint for item in pages),
        )
        != plan.ordered_binding_page_fingerprint_accumulator
    ):
        raise ValueError("legacy binding plan page set drifted")
    current_rows = tuple(
        connection.cursor(row_factory=dict_row).execute(
            """
            SELECT outbox_id, graph_id, governance_batch_id, decision_id,
                   mutation_lane, sequence_key, target_entry_key,
                   dirty_memory_ids, payload, status, attempt_count,
                   created_at, applied_at
            FROM public.memory_write_outbox
            ORDER BY sequence_key, created_at, outbox_id
            """
        ).fetchall()
    )
    root = context_fingerprint(
        "legacy-canonical-mutation-row-accumulator:v1",
        tuple(_legacy_row_fingerprint(row) for row in current_rows),
    )
    if len(current_rows) != plan.legacy_row_count or root != plan.legacy_row_accumulator:
        raise ValueError("legacy mutation rows changed after binding plan")
    return plan


def read_legacy_binding_plan(
    connection: Connection,
    *,
    maintenance_operation_id: str,
) -> tuple[
    LegacySurfaceMigrationBindingPlanFact,
    tuple[LegacySurfaceMigrationBindingEntryFact, ...],
]:
    row = connection.execute(
        """
        SELECT plan_payload
        FROM public.canonical_mutation_v2_migration_binding_plans
        WHERE maintenance_operation_id = %s
        """,
        (maintenance_operation_id,),
    ).fetchone()
    if row is None:
        raise ValueError("legacy surface binding plan is absent")
    plan = LegacySurfaceMigrationBindingPlanFact.model_validate(row[0])
    pages = tuple(
        LegacySurfaceMigrationBindingPageFact.model_validate(item[0])
        for item in connection.execute(
            """
            SELECT page_payload
            FROM public.canonical_mutation_v2_migration_binding_plan_pages
            WHERE maintenance_operation_id = %s
            ORDER BY page_index
            """,
            (maintenance_operation_id,),
        ).fetchall()
    )
    entries = tuple(
        entry for page in pages for entry in page.ordered_entries
    )
    if (
        len(entries) != plan.binding_entry_count
        or context_fingerprint(
            "legacy-surface-binding-entry-accumulator:v1",
            tuple(item.entry_fingerprint for item in entries),
        )
        != plan.binding_entry_accumulator
    ):
        raise ValueError("legacy surface binding entry set drifted")
    return plan, entries


def _coverage_set_is_complete(
    connection: Connection,
    *,
    kind: DurableProjectionKind,
    maintenance_operation_id: str | None,
    maintenance_epoch_fingerprint: str,
) -> bool:
    if not maintenance_operation_id:
        return False
    session_ids = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT id FROM public.sessions ORDER BY id"
        ).fetchall()
    )
    receipts = tuple(
        PreActivationProjectionCoverageReceiptFact.model_validate(row[0])
        for row in connection.execute(
            """
            SELECT receipt_payload
            FROM public.durable_projection_pre_activation_coverage_receipts
            WHERE projection_kind = %s
            ORDER BY runtime_session_id
            """,
            (kind.value,),
        ).fetchall()
    )
    if tuple(item.runtime_session_id for item in receipts) != session_ids:
        return False
    expected_authority = context_fingerprint(
        "runtime-write-maintenance-authority-reference:v1",
        {
            "maintenance_operation_id": maintenance_operation_id,
            "epoch_fingerprint": maintenance_epoch_fingerprint,
            "target_migration_version": _TARGET_VERSION_BY_KIND[kind],
        },
    )
    for receipt in receipts:
        if (
            receipt.maintenance_operation_id != maintenance_operation_id
            or receipt.maintenance_authority_fingerprint
            != expected_authority
            or ledger_horizon_for_session(
                connection, receipt.runtime_session_id
            )
            != receipt.frozen_horizon
        ):
            return False
    return True


def _legacy_applied_target_proof(
    connection: Connection,
    *,
    row: dict[str, Any],
    surface: CanonicalMutationSurface,
    payload_sha: str,
) -> str:
    payload = parse_legacy_mutation_payload(row["payload"])
    if surface is CanonicalMutationSurface.VECTOR_INDEX:
        ids = tuple(payload.dirty_memory_ids)
        records = tuple(
            connection.execute(
                """
                SELECT graph_id, memory_id, embedding_fingerprint,
                       embedded_text_hash, builder_version,
                       vector_dims(embedding)
                FROM public.memory_vector_index
                WHERE graph_id = %s AND memory_id = ANY(%s)
                ORDER BY memory_id
                """,
                (str(row["graph_id"]), list(ids)),
            ).fetchall()
        )
        if len(records) != len(ids):
            raise ValueError(
                "applied legacy vector surface lacks exact target authority"
            )
        target = records
    elif surface is CanonicalMutationSurface.SEARCH_INDEX:
        ids = tuple(payload.dirty_memory_ids)
        records = tuple(
            connection.execute(
                """
                SELECT graph_id, memory_id, memory_type, scope, status,
                       fts::text, aliases, updated_at
                FROM public.memory_search_index
                WHERE graph_id = %s AND memory_id = ANY(%s)
                ORDER BY memory_id
                """,
                (str(row["graph_id"]), list(ids)),
            ).fetchall()
        )
        if len(records) != len(ids):
            raise ValueError(
                "applied legacy search surface lacks exact target authority"
            )
        target = records
    else:
        # PostgreSQL cannot prove an old external Oxigraph side effect. The
        # reset-only V1 policy refuses to invent historical authority.
        raise ValueError(
            "applied legacy Oxigraph delivery requires rebuild or reset"
        )
    return context_fingerprint(
        "legacy-surface-observed-target-semantic:v1",
        {
            "outbox_id": str(row["outbox_id"]),
            "surface": surface.value,
            "payload_sha256": payload_sha,
            "target_records": target,
        },
    )


def _legacy_row_fingerprint(row: dict[str, Any]) -> str:
    payload = {
        "outbox_id": str(row["outbox_id"]),
        "graph_id": str(row["graph_id"]),
        "governance_batch_id": row["governance_batch_id"],
        "decision_id": row["decision_id"],
        "mutation_lane": str(row["mutation_lane"]),
        "sequence_key": str(row["sequence_key"]),
        "target_entry_key": str(row["target_entry_key"]),
        "dirty_memory_ids": row["dirty_memory_ids"],
        "payload": row["payload"],
        "status": str(row["status"]),
        "attempt_count": int(row["attempt_count"]),
        "created_at": _canonical_datetime(row["created_at"]),
        "applied_at": _canonical_datetime(row["applied_at"]),
    }
    return context_fingerprint("legacy-canonical-mutation-row:v1", payload)


def _payload_sha256(payload: Any) -> str:
    return "sha256:" + sha256(canonical_json_bytes(payload)).hexdigest()


def _surface_from_legacy(value: str) -> CanonicalMutationSurface:
    aliases = {
        "search_index": CanonicalMutationSurface.SEARCH_INDEX,
        "vector_index": CanonicalMutationSurface.VECTOR_INDEX,
        "oxigraph": CanonicalMutationSurface.OXIGRAPH,
    }
    try:
        return aliases[value]
    except KeyError as error:
        raise ValueError(f"unknown legacy mutation surface {value!r}") from error


def _bounded_chunks(
    entries: Iterable[LegacySurfaceMigrationBindingEntryFact],
) -> Iterable[tuple[LegacySurfaceMigrationBindingEntryFact, ...]]:
    chunk: list[LegacySurfaceMigrationBindingEntryFact] = []
    current_bytes = 0
    for entry in entries:
        encoded_bytes = len(canonical_json_bytes(entry.model_dump(mode="json")))
        if encoded_bytes > _MAX_PAGE_BYTES:
            raise ValueError("legacy surface binding entry exceeds page byte bound")
        if chunk and (
            len(chunk) >= _MAX_PAGE_ROWS
            or current_bytes + encoded_bytes > _MAX_PAGE_BYTES
        ):
            yield tuple(chunk)
            chunk = []
            current_bytes = 0
        chunk.append(entry)
        current_bytes += encoded_bytes
    if chunk:
        yield tuple(chunk)


def _canonical_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        return str(value)
    resolved = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_legacy_plan_resource() -> None:
    payload = _resource_json(_LEGACY_PLAN_RESOURCE)
    if (
        int(payload["maximum_rows_per_page"]) != _MAX_PAGE_ROWS
        or int(payload["maximum_canonical_utf8_bytes_per_page"])
        != _MAX_PAGE_BYTES
        or tuple(payload["ordered_surface_kinds"])
        != tuple(item.value for item in CanonicalMutationSurface)
    ):
        raise ValueError("legacy binding plan resource/implementation drifted")


def _resource_json(name: str) -> dict[str, Any]:
    resource = files("pulsara_agent.storage.migrations.resources").joinpath(
        name
    )
    return cast(dict[str, Any], json.loads(resource.read_text("utf-8")))


__all__ = [
    "PostgresProjectionMigrationPreparationCoordinator",
    "ProjectionMigrationPreparationReport",
    "ProjectionMigrationReadiness",
    "build_pre_activation_cutover",
    "ledger_horizon_for_session",
    "load_activation_semantic",
    "load_pre_activation_contract_semantics",
    "packaged_pre_activation_contracts",
    "projection_migration_readiness",
    "read_legacy_binding_plan",
]
