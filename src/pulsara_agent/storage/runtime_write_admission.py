"""Database-enforced runtime write admission and maintenance epochs."""

from __future__ import annotations

import json
import secrets
from importlib.resources import files
from time import monotonic
from typing import cast

from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.projection_jobs.contracts import (
    RuntimeWriteAdmissionEpochFact,
    RuntimeWriteAdmissionGuardHandle,
    RuntimeWriteAdmissionMode,
    RuntimeWriteMaintenanceAuthorityFact,
    RuntimeWriteProtectedRelationFact,
    RuntimeWriteProtectedRelationRegistryFact,
    build_projection_fact,
)


_V5_RESOURCE_NAME = "0005_runtime_write_protected_relations_v1.json"
_LATEST_RESOURCE_NAME = "0013_runtime_write_protected_relations_v1.json"


def _resource_payload(
    resource_name: str = _LATEST_RESOURCE_NAME,
) -> dict[str, object]:
    resource = files("pulsara_agent.storage.migrations.resources").joinpath(
        resource_name
    )
    payload = json.loads(resource.read_text(encoding="utf-8"))
    parent_name = payload.get("extends")
    if parent_name is None:
        return payload
    if not isinstance(parent_name, str) or parent_name == resource_name:
        raise ValueError("runtime write registry overlay parent is invalid")
    parent = _resource_payload(parent_name)
    parent_relations = tuple(parent["relations"])
    overlay_relations = tuple(payload["relations"])
    identities = {
        (str(item["schema_name"]), str(item["relation_name"]))
        for item in parent_relations
    }
    if any(
        (str(item["schema_name"]), str(item["relation_name"])) in identities
        for item in overlay_relations
    ):
        raise ValueError("runtime write registry overlay replaces an existing relation")
    return {
        "schema_version": "runtime_write_protected_relation_registry_resource.v1",
        "registry_version": payload["registry_version"],
        "relations": (*parent_relations, *overlay_relations),
    }


def _trigger_name(schema_name: str, relation_name: str) -> str:
    suffix = context_fingerprint(
        "runtime-write-guard-trigger-name:v1",
        {"schema_name": schema_name, "relation_name": relation_name},
    ).split(":", 1)[1][:16]
    return f"pulsara_runtime_write_guard_{suffix}"


def build_runtime_write_protected_relation_registry(
    *,
    resource_name: str = _LATEST_RESOURCE_NAME,
) -> RuntimeWriteProtectedRelationRegistryFact:
    payload = _resource_payload(resource_name)
    relations: list[RuntimeWriteProtectedRelationFact] = []
    raw_relations = tuple(payload["relations"])
    ordered_raw = tuple(
        sorted(
            raw_relations,
            key=lambda item: (
                str(item["schema_name"]),
                str(item["relation_name"]),
            ),
        )
    )
    for item in ordered_raw:
        schema_name = str(item["schema_name"])
        relation_name = str(item["relation_name"])
        trigger_name = _trigger_name(schema_name, relation_name)
        relations.append(
            cast(
                RuntimeWriteProtectedRelationFact,
                build_projection_fact(
                    RuntimeWriteProtectedRelationFact,
                    schema_version="runtime_write_protected_relation.v1",
                    schema_name=schema_name,
                    relation_name=relation_name,
                    allowed_normal_operations=tuple(item["normal"]),
                    allowed_maintenance_operations=tuple(item["maintenance"]),
                    owning_write_domains=tuple(item["domains"]),
                    guard_trigger_name=trigger_name,
                    guard_trigger_contract_fingerprint=context_fingerprint(
                        "runtime-write-guard-trigger-contract:v1",
                        {
                            "timing": "before",
                            "operations": ("insert", "update", "delete"),
                            "function": ("public.pulsara_assert_runtime_write_guard"),
                        },
                    ),
                ),
            )
        )
    ordered = tuple(relations)
    identities = tuple(
        {
            "schema_name": item.schema_name,
            "relation_name": item.relation_name,
            "relation_fingerprint": item.relation_fingerprint,
        }
        for item in ordered
    )
    inventory_fingerprint = context_fingerprint(
        "runtime-write-production-dml-inventory:v1",
        tuple(
            {
                "schema_name": item["schema_name"],
                "relation_name": item["relation_name"],
                "normal": tuple(item["normal"]),
                "maintenance": tuple(item["maintenance"]),
                "domains": tuple(item["domains"]),
            }
            for item in ordered_raw
        ),
    )
    return cast(
        RuntimeWriteProtectedRelationRegistryFact,
        build_projection_fact(
            RuntimeWriteProtectedRelationRegistryFact,
            schema_version="runtime_write_protected_relation_registry.v1",
            registry_version=str(payload["registry_version"]),
            ordered_relations=ordered,
            relation_count=len(ordered),
            relation_identity_accumulator=context_fingerprint(
                "runtime-write-protected-relation-identity-accumulator:v1",
                identities,
            ),
            production_dml_inventory_fingerprint=inventory_fingerprint,
        ),
    )


def build_runtime_write_epoch(
    *,
    database_target_fingerprint: str,
    epoch_number: int,
    mode: RuntimeWriteAdmissionMode,
    authorized_runtime_role: str,
    active_migration_registry_prefix_fingerprint: str,
    protected_relation_registry_fingerprint: str,
    maintenance_operation_id: str | None,
    target_migration_version: int | None,
    state_revision: int,
) -> RuntimeWriteAdmissionEpochFact:
    return cast(
        RuntimeWriteAdmissionEpochFact,
        build_projection_fact(
            RuntimeWriteAdmissionEpochFact,
            schema_version="runtime_write_admission_epoch.v1",
            database_target_fingerprint=database_target_fingerprint,
            epoch_number=epoch_number,
            mode=mode,
            authorized_runtime_role=authorized_runtime_role,
            active_migration_registry_prefix_fingerprint=(
                active_migration_registry_prefix_fingerprint
            ),
            protected_relation_registry_fingerprint=(
                protected_relation_registry_fingerprint
            ),
            maintenance_operation_id=maintenance_operation_id,
            target_migration_version=target_migration_version,
            state_revision=state_revision,
        ),
    )


def install_runtime_write_admission_v5(
    connection: Connection,
    *,
    database_target_fingerprint: str,
    runtime_role: str,
    resulting_registry_prefix_fingerprint: str,
) -> RuntimeWriteAdmissionEpochFact:
    """Install the v5 physical authority inside the migration transaction."""

    registry = build_runtime_write_protected_relation_registry(
        resource_name=_V5_RESOURCE_NAME
    )
    connection.execute(
        """
        INSERT INTO public.runtime_write_guard_secrets (
            singleton,
            guard_secret,
            authorized_runtime_role
        ) VALUES (true, %s, %s)
        """,
        (secrets.token_bytes(32), runtime_role),
    )
    for relation in registry.ordered_relations:
        connection.execute(
            """
            INSERT INTO public.runtime_write_protected_relations (
                schema_name,
                relation_name,
                relation_payload,
                relation_fingerprint,
                registry_fingerprint
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (
                relation.schema_name,
                relation.relation_name,
                Jsonb(relation.model_dump(mode="json")),
                relation.relation_fingerprint,
                registry.registry_fingerprint,
            ),
        )
        connection.execute(
            sql.SQL(
                "CREATE TRIGGER {} BEFORE INSERT OR UPDATE OR DELETE ON {}.{} "
                "FOR EACH ROW EXECUTE FUNCTION "
                "public.pulsara_assert_runtime_write_guard()"
            ).format(
                sql.Identifier(relation.guard_trigger_name),
                sql.Identifier(relation.schema_name),
                sql.Identifier(relation.relation_name),
            )
        )
    epoch = build_runtime_write_epoch(
        database_target_fingerprint=database_target_fingerprint,
        epoch_number=1,
        mode=RuntimeWriteAdmissionMode.NORMAL,
        authorized_runtime_role=runtime_role,
        active_migration_registry_prefix_fingerprint=(
            resulting_registry_prefix_fingerprint
        ),
        protected_relation_registry_fingerprint=registry.registry_fingerprint,
        maintenance_operation_id=None,
        target_migration_version=None,
        state_revision=1,
    )
    connection.execute(
        """
        INSERT INTO public.runtime_write_admission_epochs (
            singleton,
            epoch_number,
            mode,
            authorized_runtime_role,
            active_migration_registry_prefix_fingerprint,
            protected_relation_registry_fingerprint,
            maintenance_operation_id,
            target_migration_version,
            state_revision,
            epoch_payload,
            epoch_fingerprint
        ) VALUES (
            true, %s, %s, %s, %s, %s, NULL, NULL, %s, %s, %s
        )
        """,
        (
            epoch.epoch_number,
            epoch.mode.value,
            epoch.authorized_runtime_role,
            epoch.active_migration_registry_prefix_fingerprint,
            epoch.protected_relation_registry_fingerprint,
            epoch.state_revision,
            Jsonb(epoch.model_dump(mode="json")),
            epoch.epoch_fingerprint,
        ),
    )
    return epoch


def read_runtime_write_epoch(
    connection: Connection,
    *,
    privileged: bool = False,
) -> RuntimeWriteAdmissionEpochFact:
    if privileged:
        row = connection.execute(
            """
            SELECT epoch_payload
            FROM public.runtime_write_admission_epochs
            WHERE singleton
            """
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT public.pulsara_read_runtime_write_admission_epoch()"
        ).fetchone()
    if row is None:
        raise RuntimeError("runtime write admission epoch is absent")
    raw = next(iter(row.values())) if isinstance(row, dict) else row[0]
    return RuntimeWriteAdmissionEpochFact.model_validate(raw)


def acquire_normal_runtime_write_guard(
    connection: Connection,
    *,
    expected_epoch: RuntimeWriteAdmissionEpochFact,
    transaction_owner_id: str,
) -> RuntimeWriteAdmissionGuardHandle:
    row = connection.execute(
        """
        SELECT public.pulsara_acquire_normal_runtime_write_guard(%s, %s)
        """,
        (
            expected_epoch.epoch_fingerprint,
            expected_epoch.active_migration_registry_prefix_fingerprint,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("runtime write admission guard returned no authority")
    raw = next(iter(row.values())) if isinstance(row, dict) else row[0]
    observed = RuntimeWriteAdmissionEpochFact.model_validate(raw)
    if observed != expected_epoch:
        raise RuntimeError("runtime write admission epoch changed during guard acquire")
    return RuntimeWriteAdmissionGuardHandle(
        admission_epoch=observed,
        transaction_owner_id=transaction_owner_id,
        guard_lock_identity_fingerprint=context_fingerprint(
            "runtime-write-admission-guard-lock-identity:v1",
            {
                "epoch_fingerprint": observed.epoch_fingerprint,
                "transaction_owner_id": transaction_owner_id,
            },
        ),
        maintenance_authority_fingerprint=None,
    )


def acquire_maintenance_runtime_write_guard(
    connection: Connection,
    *,
    expected_epoch: RuntimeWriteAdmissionEpochFact,
    transaction_owner_id: str,
) -> RuntimeWriteAdmissionGuardHandle:
    if (
        expected_epoch.mode is not RuntimeWriteAdmissionMode.MAINTENANCE
        or expected_epoch.maintenance_operation_id is None
    ):
        raise ValueError("maintenance guard requires a maintenance epoch")
    row = connection.execute(
        """
        SELECT public.pulsara_acquire_maintenance_runtime_write_guard(%s, %s)
        """,
        (
            expected_epoch.maintenance_operation_id,
            expected_epoch.epoch_fingerprint,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("maintenance write admission guard returned no authority")
    raw = next(iter(row.values())) if isinstance(row, dict) else row[0]
    observed = RuntimeWriteAdmissionEpochFact.model_validate(raw)
    if observed != expected_epoch:
        raise RuntimeError(
            "runtime write maintenance epoch changed during guard acquire"
        )
    return RuntimeWriteAdmissionGuardHandle(
        admission_epoch=observed,
        transaction_owner_id=transaction_owner_id,
        guard_lock_identity_fingerprint=context_fingerprint(
            "runtime-write-maintenance-guard-lock-identity:v1",
            {
                "epoch_fingerprint": observed.epoch_fingerprint,
                "transaction_owner_id": transaction_owner_id,
            },
        ),
        maintenance_authority_fingerprint=context_fingerprint(
            "runtime-write-maintenance-authority-reference:v1",
            {
                "maintenance_operation_id": (observed.maintenance_operation_id),
                "epoch_fingerprint": observed.epoch_fingerprint,
                "target_migration_version": observed.target_migration_version,
            },
        ),
    )


def enter_runtime_write_maintenance(
    connection: Connection,
    *,
    current_epoch: RuntimeWriteAdmissionEpochFact,
    maintenance_operation_id: str,
    target_migration_version: int,
) -> RuntimeWriteMaintenanceAuthorityFact:
    resulting = build_runtime_write_epoch(
        database_target_fingerprint=current_epoch.database_target_fingerprint,
        epoch_number=current_epoch.epoch_number + 1,
        mode=RuntimeWriteAdmissionMode.MAINTENANCE,
        authorized_runtime_role=current_epoch.authorized_runtime_role,
        active_migration_registry_prefix_fingerprint=(
            current_epoch.active_migration_registry_prefix_fingerprint
        ),
        protected_relation_registry_fingerprint=(
            current_epoch.protected_relation_registry_fingerprint
        ),
        maintenance_operation_id=maintenance_operation_id,
        target_migration_version=target_migration_version,
        state_revision=current_epoch.state_revision + 1,
    )
    row = connection.execute(
        """
        SELECT public.pulsara_enter_runtime_write_maintenance(
            %s, %s, %s, %s, %s, %s, %s
        )
        """,
        (
            current_epoch.epoch_fingerprint,
            current_epoch.active_migration_registry_prefix_fingerprint,
            current_epoch.protected_relation_registry_fingerprint,
            maintenance_operation_id,
            target_migration_version,
            Jsonb(resulting.model_dump(mode="json")),
            resulting.epoch_fingerprint,
        ),
    ).fetchone()
    raw = (
        next(iter(row.values()))
        if isinstance(row, dict)
        else (row[0] if row is not None else None)
    )
    if row is None or RuntimeWriteAdmissionEpochFact.model_validate(raw) != resulting:
        raise RuntimeError("maintenance epoch transition did not exact-confirm")
    return cast(
        RuntimeWriteMaintenanceAuthorityFact,
        build_projection_fact(
            RuntimeWriteMaintenanceAuthorityFact,
            schema_version="runtime_write_maintenance_authority.v1",
            maintenance_operation_id=maintenance_operation_id,
            database_target_fingerprint=current_epoch.database_target_fingerprint,
            expected_normal_epoch_fingerprint=current_epoch.epoch_fingerprint,
            maintenance_epoch_fingerprint=resulting.epoch_fingerprint,
            target_migration_version=target_migration_version,
        ),
    )


def install_runtime_write_normal_epoch(
    connection: Connection,
    *,
    maintenance_epoch: RuntimeWriteAdmissionEpochFact,
    resulting_registry_prefix_fingerprint: str,
    protected_relation_resource_name: str,
) -> RuntimeWriteAdmissionEpochFact:
    if (
        maintenance_epoch.mode is not RuntimeWriteAdmissionMode.MAINTENANCE
        or maintenance_epoch.maintenance_operation_id is None
    ):
        raise ValueError("normal epoch installation requires maintenance")
    registry = build_runtime_write_protected_relation_registry(
        resource_name=protected_relation_resource_name
    )
    resulting = build_runtime_write_epoch(
        database_target_fingerprint=(maintenance_epoch.database_target_fingerprint),
        epoch_number=maintenance_epoch.epoch_number + 1,
        mode=RuntimeWriteAdmissionMode.NORMAL,
        authorized_runtime_role=maintenance_epoch.authorized_runtime_role,
        active_migration_registry_prefix_fingerprint=(
            resulting_registry_prefix_fingerprint
        ),
        protected_relation_registry_fingerprint=registry.registry_fingerprint,
        maintenance_operation_id=None,
        target_migration_version=None,
        state_revision=maintenance_epoch.state_revision + 1,
    )
    row = connection.execute(
        """
        SELECT public.pulsara_install_runtime_write_normal_epoch(
            %s, %s, %s, %s, %s, %s
        )
        """,
        (
            maintenance_epoch.maintenance_operation_id,
            maintenance_epoch.epoch_fingerprint,
            resulting_registry_prefix_fingerprint,
            registry.registry_fingerprint,
            Jsonb(resulting.model_dump(mode="json")),
            resulting.epoch_fingerprint,
        ),
    ).fetchone()
    raw = (
        next(iter(row.values()))
        if isinstance(row, dict)
        else (row[0] if row is not None else None)
    )
    if row is None or RuntimeWriteAdmissionEpochFact.model_validate(raw) != resulting:
        raise RuntimeError("normal runtime write epoch did not exact-confirm")
    return resulting


def replace_runtime_write_protected_relation_registry(
    connection: Connection,
    *,
    resource_name: str,
) -> RuntimeWriteProtectedRelationRegistryFact:
    registry = build_runtime_write_protected_relation_registry(
        resource_name=resource_name
    )
    existing = {
        (str(row[0]), str(row[1])): str(row[2])
        for row in connection.execute(
            """
            SELECT schema_name, relation_name, relation_fingerprint
            FROM public.runtime_write_protected_relations
            """
        ).fetchall()
    }
    expected = {
        (item.schema_name, item.relation_name): item
        for item in registry.ordered_relations
    }
    removed = set(existing) - set(expected)
    for schema_name, relation_name in sorted(removed):
        trigger_name = _trigger_name(schema_name, relation_name)
        relation_exists = connection.execute(
            """
            SELECT 1
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s
            """,
            (schema_name, relation_name),
        ).fetchone()
        if relation_exists is not None:
            connection.execute(
                sql.SQL("DROP TRIGGER IF EXISTS {} ON {}.{}").format(
                    sql.Identifier(trigger_name),
                    sql.Identifier(schema_name),
                    sql.Identifier(relation_name),
                )
            )
        connection.execute(
            """
            DELETE FROM public.runtime_write_protected_relations
            WHERE schema_name = %s AND relation_name = %s
            """,
            (schema_name, relation_name),
        )
    for item in registry.ordered_relations:
        connection.execute(
            """
            INSERT INTO public.runtime_write_protected_relations (
                schema_name, relation_name, relation_payload,
                relation_fingerprint, registry_fingerprint
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (schema_name, relation_name) DO UPDATE SET
                relation_payload = EXCLUDED.relation_payload,
                relation_fingerprint = EXCLUDED.relation_fingerprint,
                registry_fingerprint = EXCLUDED.registry_fingerprint
            """,
            (
                item.schema_name,
                item.relation_name,
                Jsonb(item.model_dump(mode="json")),
                item.relation_fingerprint,
                registry.registry_fingerprint,
            ),
        )
        trigger_exists = connection.execute(
            """
            SELECT 1
            FROM pg_catalog.pg_trigger t
            JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s
              AND t.tgname = %s AND NOT t.tgisinternal
            """,
            (item.schema_name, item.relation_name, item.guard_trigger_name),
        ).fetchone()
        if trigger_exists is None:
            connection.execute(
                sql.SQL(
                    "CREATE TRIGGER {} BEFORE INSERT OR UPDATE OR DELETE ON {}.{} "
                    "FOR EACH ROW EXECUTE FUNCTION "
                    "public.pulsara_assert_runtime_write_guard()"
                ).format(
                    sql.Identifier(item.guard_trigger_name),
                    sql.Identifier(item.schema_name),
                    sql.Identifier(item.relation_name),
                )
            )
    return registry


def abort_runtime_write_maintenance(
    connection: Connection,
    *,
    maintenance_epoch: RuntimeWriteAdmissionEpochFact,
) -> RuntimeWriteAdmissionEpochFact:
    if (
        maintenance_epoch.mode is not RuntimeWriteAdmissionMode.MAINTENANCE
        or maintenance_epoch.maintenance_operation_id is None
    ):
        raise ValueError("maintenance abort requires a maintenance epoch")
    resulting = build_runtime_write_epoch(
        database_target_fingerprint=(maintenance_epoch.database_target_fingerprint),
        epoch_number=maintenance_epoch.epoch_number + 1,
        mode=RuntimeWriteAdmissionMode.NORMAL,
        authorized_runtime_role=maintenance_epoch.authorized_runtime_role,
        active_migration_registry_prefix_fingerprint=(
            maintenance_epoch.active_migration_registry_prefix_fingerprint
        ),
        protected_relation_registry_fingerprint=(
            maintenance_epoch.protected_relation_registry_fingerprint
        ),
        maintenance_operation_id=None,
        target_migration_version=None,
        state_revision=maintenance_epoch.state_revision + 1,
    )
    row = connection.execute(
        """
        SELECT public.pulsara_abort_runtime_write_maintenance(
            %s, %s, %s, %s
        )
        """,
        (
            maintenance_epoch.maintenance_operation_id,
            maintenance_epoch.epoch_fingerprint,
            Jsonb(resulting.model_dump(mode="json")),
            resulting.epoch_fingerprint,
        ),
    ).fetchone()
    raw = (
        next(iter(row.values()))
        if isinstance(row, dict)
        else (row[0] if row is not None else None)
    )
    if row is None or RuntimeWriteAdmissionEpochFact.model_validate(raw) != resulting:
        raise RuntimeError("runtime write maintenance abort did not exact-confirm")
    return resulting


def require_runtime_write_epoch_matches_registry(
    connection: Connection,
    *,
    database_target_fingerprint: str,
    expected_registry_prefix_fingerprint: str,
) -> RuntimeWriteAdmissionEpochFact:
    epoch = read_runtime_write_epoch(connection)
    expected_registry = build_runtime_write_protected_relation_registry()
    if (
        epoch.mode is not RuntimeWriteAdmissionMode.NORMAL
        or epoch.database_target_fingerprint != database_target_fingerprint
        or epoch.active_migration_registry_prefix_fingerprint
        != expected_registry_prefix_fingerprint
        or epoch.protected_relation_registry_fingerprint
        != expected_registry.registry_fingerprint
    ):
        raise RuntimeError(
            "runtime write admission epoch does not match verified schema"
        )
    return epoch


def remaining_seconds(deadline_monotonic: float) -> float:
    remaining = deadline_monotonic - monotonic()
    if remaining <= 0:
        raise TimeoutError("runtime write admission deadline exceeded")
    return remaining


__all__ = [
    "abort_runtime_write_maintenance",
    "acquire_maintenance_runtime_write_guard",
    "acquire_normal_runtime_write_guard",
    "build_runtime_write_epoch",
    "build_runtime_write_protected_relation_registry",
    "enter_runtime_write_maintenance",
    "install_runtime_write_normal_epoch",
    "install_runtime_write_admission_v5",
    "read_runtime_write_epoch",
    "replace_runtime_write_protected_relation_registry",
    "require_runtime_write_epoch_matches_registry",
]
