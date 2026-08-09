#!/usr/bin/env python3
"""Generate the v13 PostgreSQL catalog from an isolated fresh database."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from time import monotonic


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CATALOG_ROOT = ROOT / "src/pulsara_agent/storage/migrations"
OUTPUT = CATALOG_ROOT / "expected_catalog_v13.json"
V3_RELATIONS = (
    "sessions",
    "session_commands",
    "turns",
    "turn_context_binding_revisions",
    "context_snapshots",
    "transcript_entries",
    "assistant_message_blocks",
    "tool_execution_attempts",
    "tool_results",
    "prompt_queue_items",
    "interaction_decisions",
    "subagent_tasks",
    "subagent_task_children",
    "durable_jobs",
    "durable_job_attempts",
    "memory_candidates",
    "memory_governance_decisions",
    "memory_facts",
    "memory_relations",
    "memory_search_index",
    "memory_vector_index",
    "memory_index_state",
    "blobs",
    "agent_events",
)


def _write(payload: dict[str, object]) -> None:
    OUTPUT.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def bootstrap() -> None:
    payload = json.loads(
        (CATALOG_ROOT / "expected_catalog_v12.json").read_text(encoding="utf-8")
    )
    for name in V3_RELATIONS:
        shape = {
            "schema_name": "pulsara_v3",
            "relation_name": name,
            "relation_kind": "r",
            "columns": [],
            "constraints": [],
            "required_index_states": [],
        }
        payload["relation_execution_shapes"].append(shape)
        payload["relations"].append({**shape, "indexes": []})
    _write(payload)


def generate() -> None:
    import psycopg

    from pulsara_agent.projection_jobs.contracts import DurableProjectionKind
    from pulsara_agent.runtime.projection_jobs.migration_port import (
        build_postgres_projection_migration_preparation_port,
    )
    from pulsara_agent.storage.migrations.catalog import PostgresCatalogCanonicalizer
    from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner
    from tests.support.postgres_database import (
        admin_root_dsn,
        create_empty_postgres_test_database,
        drop_postgres_test_database,
    )

    database = create_empty_postgres_test_database()
    try:
        coordinator = build_postgres_projection_migration_preparation_port(
            admin_dsn=database.admin_dsn,
            runtime_dsn=database.runtime_dsn,
        )
        runner = PostgresMigrationRunner(
            admin_dsn=database.admin_dsn,
            runtime_dsn=database.runtime_dsn,
            projection_preparation_port=coordinator,
        )
        deadline = monotonic() + 300.0
        while True:
            try:
                report = runner.migrate(deadline_monotonic=deadline)
            except Exception as exc:
                # The bootstrap catalog intentionally differs from the final
                # v13 database.  Final verification is expected to stop here,
                # after the atomic migration and privilege reconciliation.
                expected_bootstrap_stop = (
                    type(exc).__name__ == "PostgresSchemaError"
                    and getattr(getattr(exc, "code", None), "value", None)
                    == "schema_catalog_drift"
                ) or (
                    isinstance(exc, RuntimeError)
                    and str(exc)
                    == "packaged expected fast catalog fingerprint mismatch"
                )
                if not expected_bootstrap_stop:
                    raise
                break
            if report.migration_head_version == 5:
                coordinator.prepare_legacy_surface_bindings(
                    deadline_monotonic=deadline
                )
                continue
            if report.migration_head_version == 6:
                coordinator.drain_pre_activation(
                    kind=DurableProjectionKind.RUN_TIMELINE,
                    deadline_monotonic=deadline,
                )
                continue
            if report.migration_head_version == 7:
                coordinator.drain_pre_activation(
                    kind=DurableProjectionKind.TOOL_RESULT_EXECUTION_EVIDENCE,
                    deadline_monotonic=deadline,
                )
                continue
            if report.migration_head_version == 13:
                break
        identities = tuple(
            ("public", str(item["relation_name"]))
            for item in json.loads(
                (CATALOG_ROOT / "expected_catalog_v12.json").read_text(
                    encoding="utf-8"
                )
            )["relation_execution_shapes"]
        ) + tuple(("pulsara_v3", name) for name in V3_RELATIONS)
        with psycopg.connect(database.admin_dsn) as connection:
            canonicalizer = PostgresCatalogCanonicalizer()
            fast = canonicalizer.read_fast(connection, relation_names=identities)
            deep = canonicalizer.read_deep(connection, relation_names=identities)
        payload = {
            "schema_version": "postgres_expected_catalog.v1",
            "types": list(fast.types),
            "relation_execution_shapes": list(fast.relation_execution_shapes),
            "function_execution_shapes": list(fast.function_execution_shapes),
            "fast_executable_schema_fingerprint": fast.fast_executable_schema_fingerprint,
            "relations": list(deep.relations),
            "functions": list(deep.functions),
            "deep_catalog_fingerprint": deep.deep_catalog_fingerprint,
        }
        _write(payload)
    finally:
        drop_postgres_test_database(admin_root_dsn(), database.database_name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", action="store_true")
    args = parser.parse_args()
    if args.bootstrap:
        bootstrap()
    else:
        generate()


if __name__ == "__main__":
    main()
