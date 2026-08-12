"""Final Stage 3-5 physical-subtraction architecture gates."""

from __future__ import annotations

import ast
from dataclasses import fields
from importlib import import_module
from pathlib import Path

from pulsara_agent.conversation_kernel.job_catalog import JOB_HANDLER_CATALOG
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)
from pulsara_agent.storage.migrations.contracts import (
    BASELINE_RESOURCE,
    CATALOG_RESOURCE,
    GRANT_RESOURCE,
    UNIVERSE_GENERATION,
    UNIVERSE_ID,
)
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS
from pulsara_agent.storage.migrations.registry import POSTGRES_MIGRATION_REGISTRY
from pulsara_agent.storage.schema_contract import VerifiedPostgresSchemaBinding


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "pulsara_agent"
CLIENT = ROOT / "clients" / "terminal"


def _production_python() -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in SOURCE.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    )


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.append(node.module)
    return tuple(result)


def test_stage3_5_final_oracles_are_exact() -> None:
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 34
    assert len({item.event_type for item in COMMITTED_EVENT_DESCRIPTORS}) == 34
    assert len(LIVE_EVENT_TYPES) == 23
    assert len(SUBJECT_SLOTS) == 15
    assert len(APPEND_GUARDS) == 2
    assert len(CONVERSATION_KERNEL_RELATIONS) == 26
    assert len(set(CONVERSATION_KERNEL_RELATIONS)) == 26
    assert len(JOB_HANDLER_CATALOG) == 4
    assert len({item.handler_type for item in JOB_HANDLER_CATALOG}) == 4


def test_stage3_5_obsolete_authority_paths_are_physically_absent() -> None:
    forbidden_directories = (
        "event",
        "event_log",
        "replay",
        "runtime",
        "projection_jobs",
        "graph",
        "jsonld",
        "ontology",
        "inspector",
    )
    for relative in forbidden_directories:
        assert not tuple((SOURCE / relative).rglob("*.py")), relative

    forbidden_files = (
        SOURCE / "storage" / "runtime_write_admission.py",
        SOURCE / "storage" / "session_bootstrap.py",
        SOURCE / "terminal_protocol" / "gateway.py",
        SOURCE / "terminal_protocol" / "codec.py",
        SOURCE / "terminal_protocol" / "schema" / "terminal_client.proto",
    )
    assert not [str(path) for path in forbidden_files if path.exists()]

    for relative in (
        "internal/app",
        "internal/client",
        "internal/presentation",
        "internal/protocol",
        "internal/protocolvalue",
        "internal/wire",
    ):
        assert not tuple((CLIENT / relative).rglob("*.go")), relative


def test_stage3_5_production_import_graph_cannot_reach_deleted_authority() -> None:
    forbidden_roots = (
        "pulsara_agent.event",
        "pulsara_agent.event_log",
        "pulsara_agent.replay",
        "pulsara_agent.runtime",
        "pulsara_agent.projection_jobs",
        "pulsara_agent.graph",
        "pulsara_agent.jsonld",
        "pulsara_agent.ontology",
        "pulsara_agent.inspector",
    )
    failures: list[str] = []
    for path in _production_python():
        for imported in _imports(path):
            if any(
                imported == root or imported.startswith(root + ".")
                for root in forbidden_roots
            ):
                failures.append(f"{path.relative_to(ROOT)} -> {imported}")
    assert failures == []


def test_stage3_5_no_durable_stream_or_runtime_write_compatibility_surface() -> None:
    forbidden_tokens = (
        "RawProvider",
        "ProviderTransportSemanticDraft",
        "runtime_write_epoch",
        "runtime_write_guard_secret",
        "normal_write_admission",
        "maintenance_write_admission",
        "runtime_write_admission",
        "projection_checkpoint",
        "reconciliation_required",
        "oxigraph",
        "SPARQL",
    )
    hits: list[str] = []
    for path in _production_python():
        source = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            if token in source:
                hits.append(f"{path.relative_to(ROOT)}:{token}")
    assert hits == []


def test_stage3_5_clean_migration_universe_is_the_only_packaged_universe() -> None:
    assert UNIVERSE_ID == "pulsara.conversation-kernel.v1"
    assert UNIVERSE_GENERATION == 1
    assert POSTGRES_MIGRATION_REGISTRY.latest_version == 0

    sql_root = SOURCE / "storage" / "migrations" / "sql"
    resource_root = SOURCE / "storage" / "migrations" / "resources"
    assert tuple(sorted(path.name for path in sql_root.glob("*.sql"))) == (
        BASELINE_RESOURCE,
    )
    assert tuple(sorted(path.name for path in resource_root.glob("*.json"))) == tuple(
        sorted((CATALOG_RESOURCE, GRANT_RESOURCE))
    )

    baseline = (sql_root / BASELINE_RESOURCE).read_text(encoding="utf-8")
    assert baseline.count("CREATE TABLE pulsara_v3.") == 26
    assert "CREATE TABLE public.pulsara_schema_migrations" in baseline
    assert "CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public" in baseline
    for token in (
        "pgcrypto",
        "runtime_write",
        "maintenance_mode",
        "guard_secret",
        "projection_job",
        "agent_event_log",
    ):
        assert token not in baseline


def test_stage3_5_binding_v2_has_only_the_closed_identity_fields() -> None:
    assert tuple(item.name for item in fields(VerifiedPostgresSchemaBinding)) == (
        "database_target_fingerprint",
        "database_name",
        "database_oid",
        "normalized_search_path",
        "runtime_role",
        "server_version_num",
        "pgvector_extension_version",
        "migration_universe_id",
        "migration_universe_generation",
        "migration_universe_fingerprint",
        "migration_head_version",
        "durable_registry_prefix_fingerprint",
        "verified_catalog_fingerprint",
        "runtime_grant_policy_fingerprint",
        "verification_contract_fingerprint",
        "binding_fingerprint",
        "_construction_guard",
    )


def test_stage3_5_process_local_task_sites_are_closed() -> None:
    allowed = {
        "src/pulsara_agent/conversation_kernel/extensions.py",
        "src/pulsara_agent/conversation_kernel/host.py",
        "src/pulsara_agent/conversation_kernel/io.py",
        "src/pulsara_agent/conversation_kernel/jobs.py",
        "src/pulsara_agent/conversation_kernel/plan_runtime.py",
        "src/pulsara_agent/conversation_kernel/subagent.py",
        "src/pulsara_agent/conversation_kernel/tool_runtime.py",
        "src/pulsara_agent/terminal_client/binary.py",
    }
    observed: set[str] = set()
    for path in _production_python():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "asyncio"
                and function.attr == "create_task"
            ):
                observed.add(path.relative_to(ROOT).as_posix())
    assert observed == allowed
    jobs_source = (SOURCE / "conversation_kernel" / "jobs.py").read_text(
        encoding="utf-8"
    )
    assert "for contract in JOB_HANDLER_CATALOG" in jobs_source
    assert "self._handlers[attempt.handler_type]" in jobs_source
    assert "claim_due_job" in jobs_source


def test_stage3_5_package_facades_export_real_objects_only() -> None:
    failures: list[str] = []
    for path in _production_python():
        if path.name != "__init__.py":
            continue
        relative = path.relative_to(SOURCE).parent
        module_name = "pulsara_agent" + (
            "." + ".".join(relative.parts) if relative.parts else ""
        )
        module = import_module(module_name)
        for name in getattr(module, "__all__", ()):
            if not hasattr(module, name):
                failures.append(f"{module_name}.{name}")
    assert failures == []
