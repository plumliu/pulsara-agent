"""Closed data transforms owned by PostgreSQL migrations 0006-0008."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.runtime.projection_jobs.legacy_mutation_payload import (
    parse_legacy_mutation_payload,
)
from pulsara_agent.primitives._context_base import (
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.runtime_event_vocabulary import (
    build_bounded_runtime_failure_diagnostic,
)
from pulsara_agent.runtime.projection_jobs.postgres_canonical_mutation_repository import (
    PostgresCanonicalMutationRepository,
)
from pulsara_agent.projection_jobs.contracts import (
    CanonicalMutationKind,
    CanonicalMutationPlannedSurfaceFact,
    CanonicalMutationSurface,
    CanonicalMutationSurfaceDeliveryIdentityFact,
    CanonicalMutationSurfaceDeliveryStateFact,
    CanonicalMutationSurfacePlanFact,
    CanonicalMutationSurfaceTargetHeadFact,
    DurableProjectionKind,
    DurableProjectionKindActivationFact,
    DurableProjectionSessionCutoverFact,
    LegacyCanonicalMutationOwnerFact,
    LegacyRecordedSurfaceAppliedReceiptFact,
    LegacySurfaceHistoricalBindingProofFact,
    LegacySurfaceMigrationBindingAppliedReceiptFact,
    LegacySurfaceMigrationBindingEntryFact,
    LegacySurfaceMigrationRebindAuthorityFact,
    PreActivationProjectionCoverageReceiptFact,
    RuntimeWriteAdmissionEpochFact,
    build_projection_fact,
)
from pulsara_agent.projection_jobs.canonical_mutation import (
    build_canonical_mutation_bundle,
)
from pulsara_agent.runtime.projection_jobs.pre_activation import (
    ledger_horizon_for_session,
    load_activation_semantic,
    packaged_pre_activation_contracts,
    read_legacy_binding_plan,
)


_PROTECTED_RELATIONS_V2 = "0006_runtime_write_protected_relations_v2.json"


def apply_projection_migration_transform(
    connection: Connection,
    *,
    version: int,
    maintenance_epoch: RuntimeWriteAdmissionEpochFact,
    resulting_registry_prefix_fingerprint: str,
) -> None:
    """Apply the version's closed Python transform in the outer SQL transaction."""

    if version == 6:
        _migrate_legacy_mutations(
            connection,
            maintenance_epoch=maintenance_epoch,
            resulting_registry_prefix_fingerprint=(
                resulting_registry_prefix_fingerprint
            ),
        )
        _install_pre_activation_authority(
            connection,
            resulting_registry_prefix_fingerprint=(
                resulting_registry_prefix_fingerprint
            ),
        )
        return
    if version == 7:
        _activate_projection_kind(
            connection,
            kind=DurableProjectionKind.RUN_TIMELINE,
            migration_version=version,
            resulting_registry_prefix_fingerprint=(
                resulting_registry_prefix_fingerprint
            ),
            maintenance_epoch=maintenance_epoch,
        )
        return
    if version == 8:
        _activate_projection_kind(
            connection,
            kind=DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE,
            migration_version=version,
            resulting_registry_prefix_fingerprint=(
                resulting_registry_prefix_fingerprint
            ),
            maintenance_epoch=maintenance_epoch,
        )


def protected_relation_resource_for_version(version: int) -> str:
    return (
        "0005_runtime_write_protected_relations_v1.json"
        if version <= 5
        else _PROTECTED_RELATIONS_V2
    )


def _migrate_legacy_mutations(
    connection: Connection,
    *,
    maintenance_epoch: RuntimeWriteAdmissionEpochFact,
    resulting_registry_prefix_fingerprint: str,
) -> None:
    operation_id = maintenance_epoch.maintenance_operation_id
    if not operation_id or maintenance_epoch.target_migration_version != 6:
        raise ValueError("migration 0006 requires its exact maintenance epoch")
    _require_empty_v2_mutation_tables(connection)
    plan, entries = read_legacy_binding_plan(
        connection,
        maintenance_operation_id=operation_id,
    )
    expected_authority = _maintenance_authority_fingerprint(maintenance_epoch)
    if (
        plan.maintenance_authority_fingerprint != expected_authority
        or plan.database_target_fingerprint
        != maintenance_epoch.database_target_fingerprint
    ):
        raise ValueError("legacy binding plan maintenance authority drifted")
    by_outbox: dict[
        str, dict[CanonicalMutationSurface, LegacySurfaceMigrationBindingEntryFact]
    ] = {}
    for entry in entries:
        by_outbox.setdefault(entry.legacy_outbox_id, {})[entry.surface] = entry

    rows = tuple(
        connection.cursor(row_factory=dict_row)
        .execute(
            """
            SELECT outbox_id, graph_id, governance_batch_id, decision_id,
                   mutation_lane, sequence_key, target_entry_key,
                   dirty_memory_ids, payload, status, attempt_count,
                   last_error, created_at, applied_at
            FROM public.memory_write_outbox
            ORDER BY sequence_key, created_at, outbox_id
            """
        )
        .fetchall()
    )
    mutation_fingerprints: list[str] = []
    delivery_fingerprints: list[str] = []
    branch_counts = {
        "historical_confirmed": 0,
        "migration_rebound": 0,
        "decommission_and_rebuild": 0,
    }
    terminal_gap: dict[tuple[CanonicalMutationSurface, str], bool] = {}
    terminal_heads: dict[
        tuple[CanonicalMutationSurface, str],
        CanonicalMutationSurfaceTargetHeadFact,
    ] = {}
    for row in rows:
        outbox_id = str(row["outbox_id"])
        payload_model = parse_legacy_mutation_payload(row["payload"])
        payload_sha = _sha256_payload(row["payload"])
        entry_map = by_outbox.get(outbox_id, {})
        requested = tuple(
            _legacy_surface(name) for name in payload_model.surface_apply_status
        )
        if set(requested) != set(entry_map):
            raise ValueError("legacy binding plan does not classify every surface")
        planned = tuple(_planned_surface(entry_map[surface]) for surface in requested)
        surface_plan = cast(
            CanonicalMutationSurfacePlanFact,
            build_projection_fact(
                CanonicalMutationSurfacePlanFact,
                schema_version="canonical_mutation_surface_plan.v1",
                ordered_surfaces=planned,
                composition_fingerprint=context_fingerprint(
                    "canonical-mutation-surface-composition:v2",
                    tuple(
                        item.handler_contract.contract_fingerprint for item in planned
                    ),
                ),
            ),
        )
        owner = cast(
            LegacyCanonicalMutationOwnerFact,
            build_projection_fact(
                LegacyCanonicalMutationOwnerFact,
                schema_version="legacy_canonical_mutation_owner.v1",
                owner_kind="legacy_migration",
                legacy_outbox_id=outbox_id,
                legacy_payload_sha256=payload_sha,
                migration_version=6,
            ),
        )
        body = payload_model.model_dump(mode="json")
        body.pop("mutation_id", None)
        body.pop("surface_apply_status", None)
        bundle = build_canonical_mutation_bundle(
            source_owner=owner,
            mutation_kind=_legacy_mutation_kind(payload_model.mutation_lane.value),
            graph_id=str(row["graph_id"]),
            payloads=(body,),
            surface_plan=surface_plan,
            source_authority_fingerprints=tuple(
                entry.entry_fingerprint for entry in entry_map.values()
            ),
            mutation_ids=(outbox_id,),
        )
        receipts = PostgresCanonicalMutationRepository.append_candidates_in_transaction(
            connection,
            source_owner=owner,
            surface_plan=surface_plan,
            candidates=bundle.ordered_mutation_candidates,
        )
        if len(receipts) != 1:
            raise ValueError("legacy mutation transform cardinality drifted")
        mutation_fingerprints.append(receipts[0].mutation_fact_fingerprint)
        for surface in requested:
            entry = entry_map[surface]
            branch_counts[entry.binding_authority.binding_kind] += 1
            status = payload_model.surface_apply_status[_legacy_surface_name(surface)]
            state, mutation_sequence, sequence_key = _bind_legacy_delivery_state(
                connection,
                mutation_id=outbox_id,
                surface=surface,
                legacy_status=status,
                legacy_payload_sha256=payload_sha,
                attempt_count=int(row["attempt_count"]),
                eligible_at=cast(datetime, row["created_at"]),
            )
            delivery_fingerprints.append(
                state.delivery_identity.delivery_identity_fingerprint
            )
            key = (surface, sequence_key)
            if status == "applied":
                if terminal_gap.get(key, False):
                    raise ValueError(
                        "legacy surface ordering has applied state after a gap"
                    )
                assert state.terminal_receipt is not None
                previous = terminal_heads.get(key)
                head = cast(
                    CanonicalMutationSurfaceTargetHeadFact,
                    build_projection_fact(
                        CanonicalMutationSurfaceTargetHeadFact,
                        schema_version=("canonical_mutation_surface_target_head.v1"),
                        surface=surface,
                        sequence_key=sequence_key,
                        terminal_surface_sequence_number=(
                            state.delivery_identity.surface_sequence_number
                        ),
                        terminal_mutation_sequence_number=mutation_sequence,
                        terminal_mutation_id=outbox_id,
                        terminal_mutation_semantic_fingerprint=(
                            state.delivery_identity.mutation_semantic_fingerprint
                        ),
                        terminal_disposition="applied",
                        terminal_receipt_fingerprint=(
                            state.terminal_receipt.receipt_fingerprint
                        ),
                        head_revision=previous.head_revision + 1 if previous else 1,
                    ),
                )
                _write_surface_target_head(
                    connection, expected=previous, resulting=head
                )
                terminal_heads[key] = head
            else:
                terminal_gap[key] = True

    receipt = cast(
        LegacySurfaceMigrationBindingAppliedReceiptFact,
        build_projection_fact(
            LegacySurfaceMigrationBindingAppliedReceiptFact,
            schema_version=("legacy_surface_migration_binding_applied_receipt.v1"),
            plan_fingerprint=plan.plan_fingerprint,
            resulting_v6_registry_prefix_fingerprint=(
                resulting_registry_prefix_fingerprint
            ),
            historical_confirmed_count=branch_counts["historical_confirmed"],
            migration_rebound_count=branch_counts["migration_rebound"],
            decommissioned_and_rebuilt_count=branch_counts["decommission_and_rebuild"],
            ordered_surface_rebase_fingerprints=(),
            resulting_mutation_accumulator=context_fingerprint(
                "legacy-v2-mutation-result-accumulator:v1",
                tuple(mutation_fingerprints),
            ),
            resulting_surface_delivery_accumulator=context_fingerprint(
                "legacy-v2-surface-delivery-result-accumulator:v1",
                tuple(delivery_fingerprints),
            ),
        ),
    )
    connection.execute(
        """
        INSERT INTO public.canonical_mutation_v2_migration_binding_receipts (
            receipt_fingerprint, plan_id, receipt_payload
        ) VALUES (%s, %s, %s)
        """,
        (
            receipt.receipt_fingerprint,
            plan.plan_id,
            Jsonb(receipt.model_dump(mode="json")),
        ),
    )


def _install_pre_activation_authority(
    connection: Connection,
    *,
    resulting_registry_prefix_fingerprint: str,
) -> None:
    if connection.execute(
        "SELECT count(*) FROM public.durable_projection_kind_activations"
    ).fetchone()[0]:
        raise ValueError("v6 cannot install over active projection kinds")
    if connection.execute(
        "SELECT count(*) FROM public.durable_projection_jobs"
    ).fetchone()[0]:
        raise ValueError("v6 cannot install with preexisting projection jobs")
    contracts = packaged_pre_activation_contracts(
        resulting_v6_registry_prefix_fingerprint=(resulting_registry_prefix_fingerprint)
    )
    for contract in contracts:
        connection.execute(
            """
            INSERT INTO public.durable_projection_pre_activation_contracts (
                projection_kind, contract_payload, contract_fingerprint
            ) VALUES (%s, %s, %s)
            """,
            (
                contract.contract_semantic.projection_kind.value,
                Jsonb(contract.model_dump(mode="json")),
                contract.contract_fingerprint,
            ),
        )
    session_ids = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT id FROM public.sessions ORDER BY id"
        ).fetchall()
    )
    for runtime_session_id in session_ids:
        horizon = ledger_horizon_for_session(connection, runtime_session_id)
        for contract in contracts:
            from pulsara_agent.runtime.projection_jobs.pre_activation import (
                build_pre_activation_cutover,
            )

            cutover = build_pre_activation_cutover(
                runtime_session_id=runtime_session_id,
                contract=contract,
                horizon=horizon,
            )
            connection.execute(
                """
                INSERT INTO public.durable_projection_pre_activation_session_cutovers (
                    runtime_session_id, projection_kind,
                    cutover_payload, cutover_fingerprint
                ) VALUES (%s, %s, %s, %s)
                """,
                (
                    runtime_session_id,
                    contract.contract_semantic.projection_kind.value,
                    Jsonb(cutover.model_dump(mode="json")),
                    cutover.cutover_fingerprint,
                ),
            )


def _activate_projection_kind(
    connection: Connection,
    *,
    kind: DurableProjectionKind,
    migration_version: int,
    resulting_registry_prefix_fingerprint: str,
    maintenance_epoch: RuntimeWriteAdmissionEpochFact,
) -> None:
    operation_id = maintenance_epoch.maintenance_operation_id
    if (
        not operation_id
        or maintenance_epoch.target_migration_version != migration_version
    ):
        raise ValueError("projection activation maintenance authority drifted")
    semantic = load_activation_semantic(kind)
    contract_row = connection.execute(
        """
        SELECT contract_payload, contract_fingerprint
        FROM public.durable_projection_pre_activation_contracts
        WHERE projection_kind = %s
        """,
        (kind.value,),
    ).fetchone()
    if contract_row is None:
        raise ValueError("projection activation lacks pre-activation contract")
    from pulsara_agent.projection_jobs.contracts import (
        PreActivationProjectionHookContractFact,
    )

    pre_contract = PreActivationProjectionHookContractFact.model_validate(
        contract_row[0]
    )
    if (
        pre_contract.contract_fingerprint != str(contract_row[1])
        or pre_contract.contract_semantic.handler_contract
        != semantic.seed_contract.handler_contract
        or pre_contract.contract_semantic.delivery_policy
        != semantic.seed_contract.delivery_policy
        or pre_contract.contract_semantic.canonical_mutation_surface_plan
        != semantic.seed_contract.canonical_mutation_surface_plan
        or pre_contract.contract_semantic.ordered_trigger_bindings
        != semantic.seed_contract.ordered_trigger_bindings
        or pre_contract.contract_semantic.source_query_contract_fingerprint
        != semantic.seed_contract.source_query_contract_fingerprint
    ):
        raise ValueError("activation/pre-activation contract mismatch")
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
    session_ids = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT id FROM public.sessions ORDER BY id"
        ).fetchall()
    )
    if tuple(item.runtime_session_id for item in receipts) != session_ids:
        raise ValueError("pre-activation coverage session set is incomplete")
    maintenance_authority = _maintenance_authority_fingerprint(maintenance_epoch)
    for receipt in receipts:
        if (
            receipt.pre_activation_contract_fingerprint
            != pre_contract.contract_fingerprint
            or receipt.maintenance_operation_id != operation_id
            or receipt.maintenance_authority_fingerprint != maintenance_authority
            or ledger_horizon_for_session(connection, receipt.runtime_session_id)
            != receipt.frozen_horizon
        ):
            raise ValueError("pre-activation coverage authority drifted")
    if connection.execute(
        """
        SELECT count(*) FROM public.durable_projection_jobs
        WHERE projection_kind = %s
        """,
        (kind.value,),
    ).fetchone()[0]:
        raise ValueError("activation cannot overlap durable jobs")
    activation = cast(
        DurableProjectionKindActivationFact,
        build_projection_fact(
            DurableProjectionKindActivationFact,
            schema_version="durable_projection_kind_activation.v1",
            activation_semantic=semantic,
            activation_migration_version=migration_version,
            resulting_migration_registry_prefix_fingerprint=(
                resulting_registry_prefix_fingerprint
            ),
        ),
    )
    connection.execute(
        """
        INSERT INTO public.durable_projection_kind_activations (
            projection_kind, activation_payload, activation_fingerprint
        ) VALUES (%s, %s, %s)
        """,
        (
            kind.value,
            Jsonb(activation.model_dump(mode="json")),
            activation.activation_fingerprint,
        ),
    )
    for receipt in receipts:
        horizon = receipt.frozen_horizon
        cutover = cast(
            DurableProjectionSessionCutoverFact,
            build_projection_fact(
                DurableProjectionSessionCutoverFact,
                schema_version="durable_projection_session_cutover.v1",
                runtime_session_id=receipt.runtime_session_id,
                projection_kind=kind,
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
                migration_version=migration_version,
                migration_registry_prefix_fingerprint=(
                    resulting_registry_prefix_fingerprint
                ),
                activation_fingerprint=activation.activation_fingerprint,
                seed_contract_fingerprint=(
                    semantic.seed_contract.seed_contract_fingerprint
                ),
                cutover_policy_id="post_cutover_events_only",
            ),
        )
        connection.execute(
            """
            INSERT INTO public.durable_projection_session_cutovers (
                runtime_session_id, projection_kind,
                cutover_through_sequence, cutover_payload,
                cutover_fingerprint
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (
                cutover.runtime_session_id,
                kind.value,
                cutover.cutover_through_sequence,
                Jsonb(cutover.model_dump(mode="json")),
                cutover.cutover_fingerprint,
            ),
        )
    connection.execute(
        """
        DELETE FROM public.durable_projection_pre_activation_session_cutovers
        WHERE projection_kind = %s
        """,
        (kind.value,),
    )
    connection.execute(
        """
        DELETE FROM public.durable_projection_pre_activation_contracts
        WHERE projection_kind = %s
        """,
        (kind.value,),
    )


def _bind_legacy_delivery_state(
    connection: Connection,
    *,
    mutation_id: str,
    surface: CanonicalMutationSurface,
    legacy_status: str,
    legacy_payload_sha256: str,
    attempt_count: int,
    eligible_at: datetime,
) -> tuple[CanonicalMutationSurfaceDeliveryStateFact, int, str]:
    row = (
        connection.cursor(row_factory=dict_row)
        .execute(
            """
        SELECT d.delivery_identity, d.delivery_policy,
               d.sequence_key, m.mutation_sequence_number
        FROM public.canonical_mutation_surface_deliveries d
        JOIN public.canonical_mutations_v2 m
          ON m.mutation_id = d.mutation_id
        WHERE d.mutation_id = %s AND d.surface = %s
        FOR UPDATE
        """,
            (mutation_id, surface.value),
        )
        .fetchone()
    )
    if row is None:
        raise ValueError("legacy migration surface delivery is absent")
    identity = CanonicalMutationSurfaceDeliveryIdentityFact.model_validate(
        row["delivery_identity"]
    )
    from pulsara_agent.projection_jobs.contracts import (
        DurableProjectionDeliveryPolicyFact,
    )

    policy = DurableProjectionDeliveryPolicyFact.model_validate(row["delivery_policy"])
    terminal = None
    next_attempt_at = None
    failure = None
    if legacy_status == "applied":
        terminal = cast(
            LegacyRecordedSurfaceAppliedReceiptFact,
            build_projection_fact(
                LegacyRecordedSurfaceAppliedReceiptFact,
                schema_version="legacy_recorded_surface_applied_receipt.v1",
                receipt_kind="legacy_applied",
                mutation_id=mutation_id,
                surface=surface,
                legacy_outbox_id=mutation_id,
                legacy_payload_sha256=legacy_payload_sha256,
                legacy_recorded_status="applied",
                migration_version=6,
            ),
        )
        status = "applied"
    elif legacy_status == "failed":
        status = "retry_wait"
        resolved = (
            eligible_at
            if eligible_at.tzinfo is not None
            else eligible_at.replace(tzinfo=timezone.utc)
        )
        next_attempt_at = resolved.astimezone(timezone.utc)
        failure = build_bounded_runtime_failure_diagnostic(
            error=RuntimeError("legacy surface delivery failed"),
            redaction_profile_id=("canonical_mutation_surface_delivery_error.v1"),
        )
    elif legacy_status == "pending":
        status = "pending"
    else:
        raise ValueError("unsupported legacy delivery status")
    state = cast(
        CanonicalMutationSurfaceDeliveryStateFact,
        build_projection_fact(
            CanonicalMutationSurfaceDeliveryStateFact,
            schema_version="canonical_mutation_surface_delivery_state.v1",
            delivery_identity=identity,
            delivery_policy=policy,
            status=status,
            state_revision=1 if legacy_status != "pending" or attempt_count else 0,
            repair_generation=0,
            attempt_count=max(0, attempt_count),
            lease_generation=0,
            lease_owner_id=None,
            lease_expires_at=None,
            next_attempt_at=next_attempt_at,
            terminal_receipt=terminal,
            last_failure=failure,
        ),
    )
    connection.execute(
        """
        UPDATE public.canonical_mutation_surface_deliveries
        SET status = %s,
            state_revision = %s,
            attempt_count = %s,
            next_attempt_at = %s,
            terminal_receipt = %s,
            last_failure = %s,
            state_fingerprint = %s,
            updated_at = now()
        WHERE mutation_id = %s AND surface = %s
        """,
        (
            state.status,
            state.state_revision,
            state.attempt_count,
            state.next_attempt_at,
            Jsonb(state.terminal_receipt.model_dump(mode="json"))
            if state.terminal_receipt
            else None,
            Jsonb(state.last_failure.model_dump(mode="json"))
            if state.last_failure
            else None,
            state.state_fingerprint,
            mutation_id,
            surface.value,
        ),
    )
    return state, int(row["mutation_sequence_number"]), str(row["sequence_key"])


def _write_surface_target_head(
    connection: Connection,
    *,
    expected: CanonicalMutationSurfaceTargetHeadFact | None,
    resulting: CanonicalMutationSurfaceTargetHeadFact,
) -> None:
    if expected is None:
        row = connection.execute(
            """
            INSERT INTO public.canonical_mutation_surface_target_heads (
                surface, sequence_key, head_payload, head_fingerprint
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (surface, sequence_key) DO NOTHING
            RETURNING head_fingerprint
            """,
            (
                resulting.surface.value,
                resulting.sequence_key,
                Jsonb(resulting.model_dump(mode="json")),
                resulting.head_fingerprint,
            ),
        ).fetchone()
    else:
        row = connection.execute(
            """
            UPDATE public.canonical_mutation_surface_target_heads
            SET head_payload = %s, head_fingerprint = %s, updated_at = now()
            WHERE surface = %s AND sequence_key = %s
              AND head_fingerprint = %s
            RETURNING head_fingerprint
            """,
            (
                Jsonb(resulting.model_dump(mode="json")),
                resulting.head_fingerprint,
                resulting.surface.value,
                resulting.sequence_key,
                expected.head_fingerprint,
            ),
        ).fetchone()
    if row is None or str(row[0]) != resulting.head_fingerprint:
        raise ValueError("legacy surface target head CAS failed")


def _planned_surface(
    entry: LegacySurfaceMigrationBindingEntryFact,
) -> CanonicalMutationPlannedSurfaceFact:
    authority = entry.binding_authority
    if isinstance(authority, LegacySurfaceHistoricalBindingProofFact):
        return cast(
            CanonicalMutationPlannedSurfaceFact,
            build_projection_fact(
                CanonicalMutationPlannedSurfaceFact,
                schema_version="canonical_mutation_planned_surface.v1",
                handler_contract=authority.historical_handler_contract,
                delivery_policy=_default_policy(),
            ),
        )
    if isinstance(authority, LegacySurfaceMigrationRebindAuthorityFact):
        return authority.resulting_planned_surface
    raise ValueError(
        "decommission-and-rebuild legacy migration is not supported without "
        "its explicit rebuild receipt"
    )


def _default_policy():
    from pulsara_agent.runtime.projection_jobs.registry import (
        default_projection_delivery_policy,
    )

    return default_projection_delivery_policy()


def _require_empty_v2_mutation_tables(connection: Connection) -> None:
    relations = (
        "canonical_mutations_v2",
        "canonical_mutation_sequence_heads",
        "canonical_mutation_surface_deliveries",
        "canonical_mutation_surface_sequence_heads",
        "canonical_mutation_surface_target_heads",
    )
    for relation in relations:
        count = connection.execute(
            f"SELECT count(*) FROM public.{relation}"
        ).fetchone()[0]
        if count:
            raise ValueError(f"migration 0006 requires empty V2 relation {relation}")


def _maintenance_authority_fingerprint(
    epoch: RuntimeWriteAdmissionEpochFact,
) -> str:
    return context_fingerprint(
        "runtime-write-maintenance-authority-reference:v1",
        {
            "maintenance_operation_id": epoch.maintenance_operation_id,
            "epoch_fingerprint": epoch.epoch_fingerprint,
            "target_migration_version": epoch.target_migration_version,
        },
    )


def _legacy_mutation_kind(value: str) -> CanonicalMutationKind:
    if value == "governed_memory":
        return CanonicalMutationKind.GOVERNED_MEMORY
    if value == "graph_reset":
        return CanonicalMutationKind.GRAPH_RESET
    return CanonicalMutationKind.RUNTIME_SEMANTIC


def _legacy_surface(value: str) -> CanonicalMutationSurface:
    aliases = {
        "search_index": CanonicalMutationSurface.SEARCH_INDEX,
        "vector_index": CanonicalMutationSurface.VECTOR_INDEX,
        "oxigraph": CanonicalMutationSurface.OXIGRAPH,
    }
    try:
        return aliases[value]
    except KeyError as error:
        raise ValueError(f"unknown legacy surface {value!r}") from error


def _legacy_surface_name(surface: CanonicalMutationSurface) -> str:
    return surface.value.removesuffix(".v1")


def _sha256_payload(payload: Any) -> str:
    from hashlib import sha256

    return "sha256:" + sha256(canonical_json_bytes(payload)).hexdigest()


__all__ = [
    "apply_projection_migration_transform",
    "protected_relation_resource_for_version",
]
