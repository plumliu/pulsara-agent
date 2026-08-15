"""Closed clean-universe catalog and runtime privilege manifest."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import json

from pulsara_agent.storage.migrations.contracts import postgres_schema_fingerprint


CONVERSATION_KERNEL_RELATIONS = (
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
    "plan_workflows",
    "plan_interactions",
    "subagent_tasks",
    "subagent_task_children",
    "durable_jobs",
    "durable_job_attempts",
    "memory_candidates",
    "memory_candidate_tool_result_refs",
    "memory_candidate_basis_refs",
    "memory_facts",
    "memory_relations",
    "memory_embeddings",
    "blobs",
    "agent_events",
)

CONVERSATION_KERNEL_RUNTIME_PRIVILEGES = {
    "sessions": ("SELECT", "INSERT", "UPDATE"),
    "session_commands": ("SELECT", "INSERT"),
    "turns": ("SELECT", "INSERT", "UPDATE"),
    "turn_context_binding_revisions": ("SELECT", "INSERT"),
    "context_snapshots": ("SELECT", "INSERT"),
    "transcript_entries": ("SELECT", "INSERT"),
    "assistant_message_blocks": ("SELECT", "INSERT"),
    "tool_execution_attempts": ("SELECT", "INSERT", "UPDATE"),
    "tool_results": ("SELECT", "INSERT"),
    "prompt_queue_items": ("SELECT", "INSERT", "UPDATE"),
    "interaction_decisions": ("SELECT", "INSERT"),
    "plan_workflows": ("SELECT", "INSERT", "UPDATE"),
    "plan_interactions": ("SELECT", "INSERT", "UPDATE"),
    "subagent_tasks": ("SELECT", "INSERT", "UPDATE"),
    "subagent_task_children": ("SELECT", "INSERT"),
    "durable_jobs": ("SELECT", "INSERT", "UPDATE"),
    "durable_job_attempts": ("SELECT", "INSERT", "UPDATE"),
    "memory_candidates": ("SELECT", "INSERT", "UPDATE"),
    "memory_candidate_tool_result_refs": ("SELECT", "INSERT"),
    "memory_candidate_basis_refs": ("SELECT", "INSERT"),
    "memory_facts": ("SELECT", "INSERT", "UPDATE"),
    "memory_relations": ("SELECT", "INSERT"),
    "memory_embeddings": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    "blobs": ("SELECT", "INSERT", "DELETE"),
    "agent_events": ("SELECT", "INSERT"),
}
if tuple(CONVERSATION_KERNEL_RUNTIME_PRIVILEGES) != CONVERSATION_KERNEL_RELATIONS:
    raise RuntimeError("clean runtime grant catalog is not exhaustive")


@dataclass(frozen=True, slots=True)
class CleanPostgresSchemaManifest:
    product_relations: tuple[str, ...]
    required_extensions: tuple[dict[str, object], ...]
    required_functions: tuple[str, ...]
    observed_catalog_fingerprint: str
    manifest_fingerprint: str


def build_postgres_schema_manifest(_version: int = 0) -> CleanPostgresSchemaManifest:
    payload = json.loads(
        files("pulsara_agent.storage.migrations.resources")
        .joinpath("0000_conversation_kernel_expected_catalog_v1.json")
        .read_text(encoding="utf-8")
    )
    relations = tuple(str(item) for item in payload["product_relations"])
    if relations != CONVERSATION_KERNEL_RELATIONS:
        raise RuntimeError("clean expected catalog relation order changed")
    extensions = tuple(dict(item) for item in payload["required_extensions"])
    functions = tuple(str(item) for item in payload["required_functions"])
    observed_catalog_fingerprint = str(payload["observed_catalog_fingerprint"])
    fingerprint = postgres_schema_fingerprint(
        "pulsara:conversation-kernel-clean-catalog:v1",
        {
            "product_relations": relations,
            "required_extensions": extensions,
            "required_functions": functions,
            "observed_catalog_fingerprint": observed_catalog_fingerprint,
        },
    )
    return CleanPostgresSchemaManifest(
        relations,
        extensions,
        functions,
        observed_catalog_fingerprint,
        fingerprint,
    )


# Closed reset-detection surface from the retired public-schema universe.  It
# is deliberately not a wildcard over ``public``: unrelated application
# objects may coexist in a shared database and are neither read nor mutated.
PULSARA_LEGACY_PUBLIC_RELATION_NAMES = frozenset(
    {
        "agent_events",
        "artifacts",
        "background_derived_work_budget_accounts",
        "background_derived_work_budget_reservations",
        "background_derived_work_budget_settlements",
        "canonical_mutation_sequence_heads",
        "canonical_mutation_surface_deliveries",
        "canonical_mutation_surface_sequence_heads",
        "canonical_mutation_surface_target_heads",
        "canonical_mutation_v2_migration_binding_plan_pages",
        "canonical_mutation_v2_migration_binding_plans",
        "canonical_mutation_v2_migration_binding_receipts",
        "canonical_mutations_v2",
        "compaction_memory_extraction_result_candidates",
        "durable_projection_jobs",
        "durable_projection_kind_activations",
        "durable_projection_pre_activation_contracts",
        "durable_projection_pre_activation_coverage_pages",
        "durable_projection_pre_activation_coverage_receipts",
        "durable_projection_pre_activation_session_cutovers",
        "durable_projection_repair_actions",
        "durable_projection_result_receipts",
        "durable_projection_seed_failure_resolutions",
        "durable_projection_seed_failures",
        "durable_projection_session_cutovers",
        "durable_projection_target_authority_conflicts",
        "durable_projection_target_execution_leases",
        "durable_projection_target_heads",
        "graph_documents",
        "graph_relation_facts",
        "ledger_materialization_accounts",
        "mcp_continuation_secret_carriers",
        "memory_candidate_evidence_rejections",
        "memory_candidate_projection_outbox",
        "memory_candidates",
        "memory_governance_batch_inputs",
        "memory_governance_candidate_claims",
        "memory_governance_decisions",
        "memory_governance_event_outbox",
        "memory_nodes",
        "memory_relations",
        "memory_search_index",
        "memory_vector_index",
        "prompt_queue_accounts",
        "prompt_queue_artifact_preparation_holds",
        "prompt_queue_content_references",
        "prompt_queue_items",
        "pulsara_schema_migrations",
        "recall_traces",
        "recall_usages",
        "runs",
        "_".join(("runtime", "projection", "checkpoints")),
        "_".join(("runtime", "write", "admission", "epochs")),
        "_".join(("runtime", "write", "guard", "secrets")),
        "runtime_write_protected_relations",
        "sessions",
        "terminal_command_receipts",
        "tool_execution_records",
        "tool_result_artifacts",
        "turns",
        "working_context_summaries",
    }
)

__all__ = [
    "CONVERSATION_KERNEL_RELATIONS",
    "CONVERSATION_KERNEL_RUNTIME_PRIVILEGES",
    "PULSARA_LEGACY_PUBLIC_RELATION_NAMES",
    "CleanPostgresSchemaManifest",
    "build_postgres_schema_manifest",
]
