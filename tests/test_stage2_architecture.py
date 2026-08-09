"""Static activation gates for the Stage 2 authority hard cut."""

from __future__ import annotations

import ast
from dataclasses import asdict, fields
from hashlib import sha256
import json
import re
from pathlib import Path
import subprocess
import sys
from typing import get_args

from pulsara_agent.conversation_kernel.jobs import JOB_HANDLER_CATALOG
from pulsara_agent.conversation_kernel.limits import (
    STAGE2_LIMITS,
    STAGE2_STRUCTURAL_BUDGETS,
    Stage2RuntimeLimits,
)
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS
from pulsara_agent.storage.migrations.registry import POSTGRES_MIGRATION_REGISTRY
from pulsara_agent.storage.migrations.grants import (
    build_postgres_runtime_grant_policy,
)
from pulsara_agent.storage.migrations.manifest import build_postgres_schema_manifest
from pulsara_agent.terminal_protocol.v3_gateway import (
    PROTOCOL_MAJOR as PROTOCOL_V3_MAJOR,
    PROTOCOL_MINOR as PROTOCOL_V3_MINOR,
    PROTOCOL_SCHEMA_FINGERPRINT as PROTOCOL_V3_SCHEMA_FINGERPRINT,
)
from pulsara_agent.ports.live_agent_event import LivePayload, ProviderStreamPayload


ROOT = Path(__file__).resolve().parents[1]
KERNEL = ROOT / "src/pulsara_agent/conversation_kernel"
PROVIDER_PRODUCTION_MODULES = (
    KERNEL / "assembler.py",
    KERNEL / "runner.py",
    KERNEL / "direct_model.py",
    KERNEL / "job_model.py",
    ROOT / "src/pulsara_agent/llm/normalized_transport.py",
    ROOT / "src/pulsara_agent/llm/adapters/openai/events.py",
    ROOT / "src/pulsara_agent/llm/adapters/openai/responses.py",
    ROOT / "src/pulsara_agent/llm/adapters/openai/chat_completions.py",
    ROOT / "src/pulsara_agent/ports/provider_stream.py",
    ROOT / "src/pulsara_agent/ports/live_agent_event.py",
)


def test_stage2_registry_schema_and_job_catalog_are_exact() -> None:
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 26
    assert len(LIVE_EVENT_TYPES) == 23
    assert len(SUBJECT_SLOTS) == 13
    assert len(APPEND_GUARDS) == 2
    assert len(CONVERSATION_KERNEL_RELATIONS) == 24
    assert {item.handler_type for item in JOB_HANDLER_CATALOG} == {
        "BACKGROUND_COMPACTION",
        "POST_COMPACTION_MEMORY_EXTRACTION",
        "MEMORY_GOVERNANCE",
        "MEMORY_INDEX_REFRESH",
    }
    assert not any(
        token in item.handler_type.lower()
        for item in JOB_HANDLER_CATALOG
        for token in ("terminal", "subagent", "extension")
    )

    policy = build_postgres_runtime_grant_policy(13)
    expected_legacy_relations = {
        str(item["relation_name"])
        for item in build_postgres_schema_manifest(12).owned_relations
        if str(item["schema_name"]) == "public"
        and str(item["relation_name"]) != "pulsara_schema_migrations"
    }
    assert {
        item.target.relation_name
        for item in policy.revocations
        if item.target.target_kind == "relation"
    } == expected_legacy_relations


def test_stage2_kernel_has_no_legacy_authority_import_or_target_vocabulary() -> None:
    forbidden_imports = {
        "pulsara_agent.event_log",
        "pulsara_agent.runtime.session",
        "pulsara_agent.runtime.terminal_presentation",
        "pulsara_agent.projection_jobs",
        "pulsara_agent.runtime.projection_jobs",
        "pulsara_agent.memory.oxigraph",
    }
    forbidden_vocabulary = {
        "CustomEvent",
        "ToolOutcomeUnknown",
        "RawProvider",
        "CommittedEventSettlementReceipt",
    }
    for path in sorted(KERNEL.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(item.name for item in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        assert not any(
            value == prefix or value.startswith(prefix + ".")
            for value in imports
            for prefix in forbidden_imports
        ), path
        assert not any(value in source for value in forbidden_vocabulary), path


def test_stage2_provider_production_graph_has_one_live_vocabulary_and_no_adoption() -> (
    None
):
    forbidden_modules = {
        "pulsara_agent.llm.raw_provider",
        "pulsara_agent.llm.drafts",
        "pulsara_agent.llm.sanitizing_transport",
    }
    forbidden_tokens = {
        "RawProvider",
        "ProviderTransportSemanticDraft",
        "SanitizedProviderSemanticEnvelope",
        "require_adoptable",
        "acknowledge_adopted",
        "discard_unadopted",
    }
    for path in PROVIDER_PRODUCTION_MODULES:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert imports.isdisjoint(forbidden_modules), path
        assert not any(token in source for token in forbidden_tokens), path

    provider_types = set(get_args(ProviderStreamPayload))
    assert len(provider_types) == 12
    assert provider_types < set(get_args(LivePayload))
    assert not (KERNEL / "live_payloads.py").exists()


def test_stage2_production_imports_do_not_initialize_legacy_authority_graphs() -> None:
    """Catch eager package facades that defeat a clean direct-import AST."""

    script = """
import json
import sys
import pulsara_agent.conversation_kernel.host
import pulsara_agent.terminal_client.v3_launcher

forbidden = (
    "pulsara_agent.event",
    "pulsara_agent.event_log",
    "pulsara_agent.graph.oxigraph",
    "pulsara_agent.llm.drafts",
    "pulsara_agent.llm.raw_provider",
    "pulsara_agent.llm.sanitizing_transport",
    "pulsara_agent.runtime.session",
    "pulsara_agent.runtime.terminal_presentation",
    "pulsara_agent.terminal_protocol.gateway",
)
observed = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
)
print(json.dumps(observed))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == []


def test_stage2_repository_sql_is_schema_qualified_and_has_no_durable_stream() -> None:
    repository = (KERNEL / "repository.py").read_text(encoding="utf-8")
    legacy_short_names = {
        "sessions",
        "session_commands",
        "turns",
        "transcript_entries",
        "assistant_message_blocks",
        "tool_execution_attempts",
        "tool_results",
        "prompt_queue_items",
        "durable_jobs",
        "durable_job_attempts",
        "agent_events",
    }
    for match in re.finditer(
        r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+([a-z_][a-z0-9_.]*)",
        repository,
        flags=re.IGNORECASE,
    ):
        observed = match.group(1).lower()
        assert observed not in legacy_short_names, match.group(0)
    migration = (
        ROOT
        / "src/pulsara_agent/storage/migrations/sql/0013_conversation_kernel_hard_cut.sql"
    ).read_text(encoding="utf-8")
    assert "stream_segment" not in migration.lower()
    assert "coalescing" not in migration.lower()
    assert "def complete_turn(" not in repository


def test_stage2_activation_evidence_is_derived_from_code_owned_contracts() -> None:
    report = json.loads(
        (
            ROOT
            / "benchmarks/suites/core/v1/durability_subtraction_stage2_activation.json"
        ).read_text(encoding="utf-8")
    )
    assert report["schema_version"] == "durability-subtraction-stage2-activation.v1"
    assert report["status"] in {"activation_candidate", "activated"}
    assert report["authority_activation"] == "single_reset_only"
    for name, expected in report["document_sha256"].items():
        assert sha256((ROOT / name).read_bytes()).hexdigest() == expected

    latest = POSTGRES_MIGRATION_REGISTRY.definition(13)
    manifest = build_postgres_schema_manifest(13)
    assert report["schema"] == {
        "migration_head": 13,
        "product_schema": "pulsara_v3",
        "active_product_relations": len(CONVERSATION_KERNEL_RELATIONS),
        "manifest_fingerprint": manifest.manifest_fingerprint,
        "migration_registry_prefix_fingerprint": latest.registry_prefix_fingerprint,
    }
    assert report["vocabulary"] == {
        "committed": len(COMMITTED_EVENT_DESCRIPTORS),
        "live": len(LIVE_EVENT_TYPES),
        "subject_slots": len(SUBJECT_SLOTS),
        "append_guards": len(APPEND_GUARDS),
    }
    assert report["protocol"] == {
        "major": PROTOCOL_V3_MAJOR,
        "minor": PROTOCOL_V3_MINOR,
        "schema_fingerprint": PROTOCOL_V3_SCHEMA_FINGERPRINT,
    }
    assert report["runtime_limits"] == {
        "contract": "stage2_runtime_limits.v1",
        "named_finite_fields": len(fields(Stage2RuntimeLimits)),
    }
    assert all(value > 0 for value in asdict(STAGE2_LIMITS).values())
    assert report["structural_budgets"] == {
        "contract": "stage2_structural_budgets.v1",
        **asdict(STAGE2_STRUCTURAL_BUDGETS),
    }


def test_stage2_ordinary_host_and_terminal_binary_select_only_kernel_v3() -> None:
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.host import HostCore
    from pulsara_agent.terminal_client import launch_terminal_client
    from pulsara_agent.terminal_client.v3_launcher import (
        launch_terminal_kernel_client,
    )

    assert HostCore is KernelHostCore
    assert launch_terminal_client is launch_terminal_kernel_client
    cli = (ROOT / "src/pulsara_agent/cli.py").read_text(encoding="utf-8")
    assert "result = asyncio.run(_kernel_host_run(args))" in cli
    assert "asyncio.run(_kernel_host_repl(args))" in cli
    assert "asyncio.run(_kernel_host_tui(args))" in cli
    binary = (ROOT / "clients/terminal/cmd/pulsara-tui/main.go").read_text(
        encoding="utf-8"
    )
    assert "kernelbootstrap.Run" in binary
    assert "internal/bootstrap" not in binary
    launcher = (ROOT / "src/pulsara_agent/terminal_client/v3_launcher.py").read_text(
        encoding="utf-8"
    )
    assert "TerminalKernelProtocolServer" in launcher
    assert "terminal_presentation" not in launcher


def test_stage2_protocol_v3_does_not_expose_durable_event_or_blob_identity() -> None:
    proto = (
        ROOT / "src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto"
    ).read_text(encoding="utf-8")
    assert "package pulsara.terminal.v3;" in proto
    assert "StoredCommittedEvent" not in proto
    assert "RawStoredEvent" not in proto
    assert "string blob_id" not in proto
    assert "private_url" not in proto


def test_stage2_extension_and_tool_policy_have_single_production_owners() -> None:
    repository = (KERNEL / "repository.py").read_text(encoding="utf-8")
    runner = (KERNEL / "runner.py").read_text(encoding="utf-8")
    extensions = (KERNEL / "extensions.py").read_text(encoding="utf-8")
    tool_runtime = (KERNEL / "tool_runtime.py").read_text(encoding="utf-8")
    tool_policy = (KERNEL / "tool_policy.py").read_text(encoding="utf-8")

    assert "post_commit_tap" in repository
    assert "_finish_event_batch(committed=exc_type is None)" in repository
    assert "PostCommitHookOffer" not in runner
    assert "_offer_post_commit" not in runner
    assert "ConversationKernelRepository" not in extensions
    assert "ToolDispatchAuthorizationPolicy" in tool_runtime
    assert "class DefaultToolDispatchAuthorizationPolicy" in tool_policy
    assert "PolicyPermissionGate" not in tool_runtime


def test_stage2_provider_admission_and_blob_gc_are_physical_not_heuristic() -> None:
    direct = (KERNEL / "direct_model.py").read_text(encoding="utf-8")
    jobs = (KERNEL / "job_model.py").read_text(encoding="utf-8")
    reader = (KERNEL / "reader.py").read_text(encoding="utf-8")
    blob = (KERNEL / "blob.py").read_text(encoding="utf-8")
    host = (KERNEL / "host.py").read_text(encoding="utf-8")

    for source in (direct, jobs):
        assert "estimate_model_context_for_call" in source
        assert "validate_model_context_for_call" in source
        assert "canonical_bytes / 4" not in source
    assert "CanonicalProviderContinuityError" in reader
    assert "delete_orphans" in blob
    assert "kernel-blob-orphan-gc" in host
