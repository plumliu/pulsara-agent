"""Cumulative Pulsara-owned PostgreSQL object manifests."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files

from pulsara_agent.storage.migrations.contracts import (
    PostgresObjectIdentityFact,
    PostgresSchemaObjectManifest,
    postgres_schema_fingerprint,
)


_RUNTIME_RELATIONS = (
    "agent_events",
    "artifacts",
    "ledger_materialization_accounts",
    "runs",
    "runtime_projection_checkpoints",
    "sessions",
    "tool_execution_records",
    "tool_result_artifacts",
    "turns",
    "working_context_summaries",
)

_MEMORY_RELATIONS_V3 = (
    "graph_documents",
    "memory_governance_event_outbox",
    "memory_nodes",
    "memory_relations",
    "memory_search_index",
    "memory_vector_index",
    "memory_write_outbox",
    "recall_traces",
    "recall_usages",
)
_MEMORY_RELATIONS_V6 = tuple(
    name for name in _MEMORY_RELATIONS_V3 if name != "memory_write_outbox"
)

_GOVERNANCE_RELATIONS = (
    "memory_candidate_evidence_rejections",
    "memory_candidate_projection_outbox",
    "memory_candidates",
    "memory_governance_batch_inputs",
    "memory_governance_candidate_claims",
    "memory_governance_decisions",
)

_DURABLE_PROJECTION_RELATIONS = (
    "runtime_write_guard_secrets",
    "runtime_write_admission_epochs",
    "runtime_write_protected_relations",
    "durable_projection_kind_activations",
    "durable_projection_pre_activation_contracts",
    "durable_projection_pre_activation_session_cutovers",
    "durable_projection_pre_activation_coverage_pages",
    "durable_projection_pre_activation_coverage_receipts",
    "durable_projection_session_cutovers",
    "durable_projection_seed_failures",
    "durable_projection_seed_failure_resolutions",
    "durable_projection_jobs",
    "durable_projection_result_receipts",
    "durable_projection_target_heads",
    "durable_projection_target_authority_conflicts",
    "durable_projection_target_execution_leases",
    "graph_relation_facts",
    "canonical_mutations_v2",
    "canonical_mutation_sequence_heads",
    "canonical_mutation_surface_deliveries",
    "canonical_mutation_surface_sequence_heads",
    "canonical_mutation_surface_target_heads",
    "canonical_mutation_v2_migration_binding_plan_pages",
    "canonical_mutation_v2_migration_binding_plans",
    "canonical_mutation_v2_migration_binding_receipts",
    "durable_projection_repair_actions",
)

_COMPACTION_MEMORY_EXTRACTION_RELATIONS = (
    "background_derived_work_budget_accounts",
    "background_derived_work_budget_reservations",
    "background_derived_work_budget_settlements",
    "compaction_memory_extraction_result_candidates",
)

_MCP_CONTINUATION_RELATIONS = ("mcp_continuation_secret_carriers",)

_TERMINAL_PRESENTATION_QUEUE_RELATIONS = (
    "prompt_queue_accounts",
    "prompt_queue_artifact_preparation_holds",
    "prompt_queue_content_references",
    "prompt_queue_items",
    "terminal_command_receipts",
)

_RELATIONS_INTRODUCED_BY_VERSION = (
    ("pulsara_schema_migrations",),
    (),
    _RUNTIME_RELATIONS,
    _MEMORY_RELATIONS_V3,
    _GOVERNANCE_RELATIONS,
    _DURABLE_PROJECTION_RELATIONS,
    (),
    (),
    (),
    _COMPACTION_MEMORY_EXTRACTION_RELATIONS,
    _MCP_CONTINUATION_RELATIONS,
    _TERMINAL_PRESENTATION_QUEUE_RELATIONS,
)
_ALL_RELATIONS = tuple(
    name
    for introduced_relations in _RELATIONS_INTRODUCED_BY_VERSION
    for name in introduced_relations
)

RUNTIME_TRUTH_TABLES = _RUNTIME_RELATIONS
MEMORY_SUBSTRATE_TABLES = _MEMORY_RELATIONS_V6


@lru_cache(maxsize=None)
def _packaged_expected_catalog(version: int) -> dict[str, object]:
    if version <= 4:
        catalog_version = 4
    else:
        catalog_version = version
    resource = files("pulsara_agent.storage.migrations").joinpath(
        f"expected_catalog_v{catalog_version}.json"
    )
    return json.loads(resource.read_text(encoding="utf-8"))


def _freeze(value: object) -> object:
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        return {str(key): _freeze(item) for key, item in value.items()}
    return value


def _relation(
    name: str,
    *,
    writable: bool,
    through_version: int,
    runtime_privileges: tuple[str, ...] | None = None,
) -> dict[str, object]:
    expected = _packaged_expected_catalog(through_version)
    matches = tuple(
        relation
        for relation in expected["relations"]
        if relation["relation_name"] == name
    )
    if len(matches) != 1:
        raise RuntimeError(f"packaged catalog is missing exact relation {name}")
    result = {
        **_freeze(matches[0]),
        "runtime_writable": writable,
    }
    if runtime_privileges is not None:
        result["runtime_privileges"] = runtime_privileges
    return result


_V5_RUNTIME_RELATION_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "runtime_write_guard_secrets": (),
    "runtime_write_admission_epochs": ("SELECT",),
    "runtime_write_protected_relations": (),
    "durable_projection_kind_activations": ("SELECT",),
    "durable_projection_pre_activation_contracts": ("SELECT",),
    "durable_projection_pre_activation_session_cutovers": ("SELECT", "INSERT"),
    "durable_projection_pre_activation_coverage_pages": ("SELECT",),
    "durable_projection_pre_activation_coverage_receipts": ("SELECT",),
    "durable_projection_session_cutovers": ("SELECT", "INSERT"),
    "durable_projection_seed_failures": ("SELECT", "INSERT"),
    "durable_projection_seed_failure_resolutions": ("SELECT", "INSERT"),
    "durable_projection_jobs": ("SELECT", "INSERT", "UPDATE"),
    "durable_projection_result_receipts": ("SELECT", "INSERT"),
    "durable_projection_target_heads": ("SELECT", "INSERT", "UPDATE"),
    "durable_projection_target_authority_conflicts": ("SELECT", "INSERT"),
    "durable_projection_target_execution_leases": ("SELECT", "INSERT", "UPDATE"),
    "graph_relation_facts": ("SELECT", "INSERT"),
    "canonical_mutations_v2": ("SELECT", "INSERT"),
    "canonical_mutation_sequence_heads": ("SELECT", "INSERT", "UPDATE"),
    "canonical_mutation_surface_deliveries": ("SELECT", "INSERT", "UPDATE"),
    "canonical_mutation_surface_sequence_heads": ("SELECT", "INSERT", "UPDATE"),
    "canonical_mutation_surface_target_heads": ("SELECT", "INSERT", "UPDATE"),
    "canonical_mutation_v2_migration_binding_plan_pages": ("SELECT",),
    "canonical_mutation_v2_migration_binding_plans": ("SELECT",),
    "canonical_mutation_v2_migration_binding_receipts": ("SELECT",),
    "durable_projection_repair_actions": ("SELECT", "INSERT"),
}

_V9_RUNTIME_RELATION_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "background_derived_work_budget_accounts": ("SELECT", "INSERT", "UPDATE"),
    "background_derived_work_budget_reservations": (
        "SELECT",
        "INSERT",
        "UPDATE",
    ),
    "background_derived_work_budget_settlements": ("SELECT", "INSERT"),
    "compaction_memory_extraction_result_candidates": ("SELECT", "INSERT"),
}

_V10_RUNTIME_RELATION_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "mcp_continuation_secret_carriers": (
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
    ),
}

_V11_RUNTIME_RELATION_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "prompt_queue_accounts": ("SELECT", "INSERT", "UPDATE"),
    "prompt_queue_artifact_preparation_holds": (
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
    ),
    "prompt_queue_content_references": ("SELECT", "INSERT", "DELETE"),
    "prompt_queue_items": ("SELECT", "INSERT", "UPDATE"),
    "terminal_command_receipts": ("SELECT", "INSERT", "UPDATE"),
}

_V5_FUNCTION_ARGUMENT_TYPES: dict[str, tuple[str, ...]] = {
    "pulsara_jsonb_text_array": ("pg_catalog.jsonb",),
    "pulsara_runtime_write_lock_key": ("pg_catalog.text", "pg_catalog.jsonb"),
    "pulsara_acquire_normal_runtime_write_guard": (
        "pg_catalog.text",
        "pg_catalog.text",
    ),
    "pulsara_read_runtime_write_admission_epoch": (),
    "pulsara_acquire_maintenance_runtime_write_guard": (
        "pg_catalog.text",
        "pg_catalog.text",
    ),
    "pulsara_enter_runtime_write_maintenance": (
        "pg_catalog.text",
        "pg_catalog.text",
        "pg_catalog.text",
        "pg_catalog.text",
        "pg_catalog.int4",
        "pg_catalog.jsonb",
        "pg_catalog.text",
    ),
    "pulsara_install_runtime_write_normal_epoch": (
        "pg_catalog.text",
        "pg_catalog.text",
        "pg_catalog.text",
        "pg_catalog.text",
        "pg_catalog.jsonb",
        "pg_catalog.text",
    ),
    "pulsara_abort_runtime_write_maintenance": (
        "pg_catalog.text",
        "pg_catalog.text",
        "pg_catalog.jsonb",
        "pg_catalog.text",
    ),
    "pulsara_assert_runtime_write_guard": (),
}

_V5_RUNTIME_EXECUTABLE_FUNCTIONS = {
    "pulsara_jsonb_text_array",
    "pulsara_acquire_normal_runtime_write_guard",
    "pulsara_read_runtime_write_admission_epoch",
}


def _manifest_payload(through_version: int) -> dict[str, object]:
    if through_version < 0 or through_version > 11:
        raise ValueError("unsupported manifest version")
    relations: list[dict[str, object]] = [
        _relation(
            "pulsara_schema_migrations",
            writable=False,
            through_version=through_version,
        )
    ]
    if through_version >= 2:
        relations.extend(
            _relation(name, writable=True, through_version=through_version)
            for name in _RUNTIME_RELATIONS
        )
    if through_version >= 3:
        memory_relations = (
            _MEMORY_RELATIONS_V3 if through_version <= 5 else _MEMORY_RELATIONS_V6
        )
        relations.extend(
            _relation(name, writable=True, through_version=through_version)
            for name in memory_relations
        )
    if through_version >= 4:
        relations.extend(
            _relation(name, writable=True, through_version=through_version)
            for name in _GOVERNANCE_RELATIONS
        )
    if through_version >= 5:
        relations.extend(
            _relation(
                name,
                writable=any(
                    privilege in {"INSERT", "UPDATE", "DELETE"}
                    for privilege in _V5_RUNTIME_RELATION_PRIVILEGES[name]
                ),
                through_version=through_version,
                runtime_privileges=_V5_RUNTIME_RELATION_PRIVILEGES[name],
            )
            for name in _DURABLE_PROJECTION_RELATIONS
        )
    if through_version >= 9:
        relations.extend(
            _relation(
                name,
                writable=any(
                    privilege in {"INSERT", "UPDATE", "DELETE"}
                    for privilege in _V9_RUNTIME_RELATION_PRIVILEGES[name]
                ),
                through_version=through_version,
                runtime_privileges=_V9_RUNTIME_RELATION_PRIVILEGES[name],
            )
            for name in _COMPACTION_MEMORY_EXTRACTION_RELATIONS
        )
    if through_version >= 10:
        relations.extend(
            _relation(
                name,
                writable=True,
                through_version=through_version,
                runtime_privileges=_V10_RUNTIME_RELATION_PRIVILEGES[name],
            )
            for name in _MCP_CONTINUATION_RELATIONS
        )
    if through_version >= 11:
        relations.extend(
            _relation(
                name,
                writable=True,
                through_version=through_version,
                runtime_privileges=_V11_RUNTIME_RELATION_PRIVILEGES[name],
            )
            for name in _TERMINAL_PRESENTATION_QUEUE_RELATIONS
        )
    extensions: tuple[dict[str, object], ...] = ()
    if through_version >= 1:
        extensions += (
            {
                "schema_name": "public",
                "extension_name": "vector",
                "minimum_version": "0.5.0",
            },
        )
    if through_version >= 5:
        extensions += (
            {
                "schema_name": "public",
                "extension_name": "pgcrypto",
                "minimum_version": "1.0",
            },
        )
    expected = _packaged_expected_catalog(through_version)
    required_types = (
        tuple(_freeze(item) for item in expected["types"])
        if through_version >= 1
        else ()
    )
    functions: tuple[dict[str, object], ...] = ()
    if through_version >= 3:
        if through_version <= 4:
            functions = tuple(
                {
                    **_freeze(item),
                    "ordered_argument_types": ("pg_catalog.jsonb",),
                }
                for item in expected["functions"]
            )
        else:
            functions = tuple(
                {
                    **_freeze(item),
                    "ordered_argument_types": _V5_FUNCTION_ARGUMENT_TYPES[
                        str(item["function_name"])
                    ],
                    "runtime_executable": (
                        str(item["function_name"]) in _V5_RUNTIME_EXECUTABLE_FUNCTIONS
                    ),
                }
                for item in expected["functions"]
            )
    historical_relation_names = tuple(
        name
        for introduced_relations in _RELATIONS_INTRODUCED_BY_VERSION[
            : through_version + 1
        ]
        for name in introduced_relations
    )
    reserved_names = tuple(
        PostgresObjectIdentityFact.build(
            object_kind="relation", schema_name="public", object_name=name
        )
        for name in historical_relation_names
    )
    if through_version >= 3:
        reserved_names += tuple(
            PostgresObjectIdentityFact.build(
                object_kind="function",
                schema_name=str(function["schema_name"]),
                object_name=(
                    f"{function['function_name']}("
                    + ",".join(function["ordered_argument_types"])
                    + ")"
                ),
            )
            for function in functions
        )
    return {
        "schema_version": "postgres_schema_object_manifest.v1",
        "through_version": through_version,
        "required_extensions": extensions,
        "required_types": required_types,
        "owned_relations": tuple(relations),
        "required_functions": functions,
        "reserved_object_names": reserved_names,
    }


def build_postgres_schema_manifest(
    through_version: int,
) -> PostgresSchemaObjectManifest:
    payload = _manifest_payload(through_version)
    fingerprint = postgres_schema_fingerprint(
        "pulsara:postgres-schema-object-manifest:v1", payload
    )
    return PostgresSchemaObjectManifest(
        **payload,
        manifest_fingerprint=fingerprint,
    )


POSTGRES_SCHEMA_MANIFESTS = tuple(
    build_postgres_schema_manifest(version) for version in range(12)
)
POSTGRES_LATEST_SCHEMA_MANIFEST = POSTGRES_SCHEMA_MANIFESTS[-1]
PULSARA_RESERVED_RELATION_NAMES = frozenset(_ALL_RELATIONS)


__all__ = [
    "POSTGRES_LATEST_SCHEMA_MANIFEST",
    "POSTGRES_SCHEMA_MANIFESTS",
    "MEMORY_SUBSTRATE_TABLES",
    "PULSARA_RESERVED_RELATION_NAMES",
    "RUNTIME_TRUTH_TABLES",
    "build_postgres_schema_manifest",
]
