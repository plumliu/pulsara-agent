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

_MEMORY_RELATIONS = (
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

_GOVERNANCE_RELATIONS = (
    "memory_candidate_evidence_rejections",
    "memory_candidate_projection_outbox",
    "memory_candidates",
    "memory_governance_batch_inputs",
    "memory_governance_candidate_claims",
    "memory_governance_decisions",
)

_RELATIONS_INTRODUCED_BY_VERSION = (
    ("pulsara_schema_migrations",),
    (),
    _RUNTIME_RELATIONS,
    _MEMORY_RELATIONS,
    _GOVERNANCE_RELATIONS,
)
_ALL_RELATIONS = tuple(
    name
    for introduced_relations in _RELATIONS_INTRODUCED_BY_VERSION
    for name in introduced_relations
)

RUNTIME_TRUTH_TABLES = _RUNTIME_RELATIONS
MEMORY_SUBSTRATE_TABLES = _MEMORY_RELATIONS


@lru_cache(maxsize=1)
def _packaged_expected_catalog() -> dict[str, object]:
    resource = files("pulsara_agent.storage.migrations").joinpath(
        "expected_catalog_v4.json"
    )
    return json.loads(resource.read_text(encoding="utf-8"))


def _freeze(value: object) -> object:
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        return {str(key): _freeze(item) for key, item in value.items()}
    return value


def _relation(name: str, *, writable: bool) -> dict[str, object]:
    expected = _packaged_expected_catalog()
    matches = tuple(
        relation
        for relation in expected["relations"]
        if relation["relation_name"] == name
    )
    if len(matches) != 1:
        raise RuntimeError(f"packaged catalog is missing exact relation {name}")
    return {
        **_freeze(matches[0]),
        "runtime_writable": writable,
    }


def _manifest_payload(through_version: int) -> dict[str, object]:
    if through_version < 0 or through_version > 4:
        raise ValueError("unsupported manifest version")
    relations: list[dict[str, object]] = [
        _relation("pulsara_schema_migrations", writable=False)
    ]
    if through_version >= 2:
        relations.extend(_relation(name, writable=True) for name in _RUNTIME_RELATIONS)
    if through_version >= 3:
        relations.extend(_relation(name, writable=True) for name in _MEMORY_RELATIONS)
    if through_version >= 4:
        relations.extend(_relation(name, writable=True) for name in _GOVERNANCE_RELATIONS)
    extensions = (
        ({"schema_name": "public", "extension_name": "vector", "minimum_version": "0.5.0"},)
        if through_version >= 1
        else ()
    )
    expected = _packaged_expected_catalog()
    required_types = (
        tuple(_freeze(item) for item in expected["types"])
        if through_version >= 1
        else ()
    )
    functions = (
        tuple(
            {
                **_freeze(item),
                "ordered_argument_types": ("pg_catalog.jsonb",),
            }
            for item in expected["functions"]
        )
        if through_version >= 3
        else ()
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
        reserved_names += (
            PostgresObjectIdentityFact.build(
                object_kind="function",
                schema_name="public",
                object_name="pulsara_jsonb_text_array(pg_catalog.jsonb)",
            ),
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


def build_postgres_schema_manifest(through_version: int) -> PostgresSchemaObjectManifest:
    payload = _manifest_payload(through_version)
    fingerprint = postgres_schema_fingerprint(
        "pulsara:postgres-schema-object-manifest:v1", payload
    )
    return PostgresSchemaObjectManifest(
        **payload,
        manifest_fingerprint=fingerprint,
    )


POSTGRES_SCHEMA_MANIFESTS = tuple(
    build_postgres_schema_manifest(version) for version in range(5)
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
