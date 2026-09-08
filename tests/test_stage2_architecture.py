"""Static activation gates for the Stage 2 authority hard cut."""

from __future__ import annotations

import ast
from dataclasses import asdict, fields
import json
import re
from pathlib import Path
import subprocess
import sys
from typing import get_args

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
from pulsara_agent.storage.migrations.manifest import (
    CONVERSATION_KERNEL_RELATIONS,
    CONVERSATION_KERNEL_RUNTIME_PRIVILEGES,
)
from pulsara_agent.storage.migrations.registry import POSTGRES_MIGRATION_REGISTRY
from pulsara_agent.storage.migrations.grants import (
    build_postgres_runtime_grant_policy,
)
from pulsara_agent.storage.migrations.manifest import build_postgres_schema_manifest
from pulsara_agent.ports.live_agent_event import LivePayload, ProviderStreamPayload


ROOT = Path(__file__).resolve().parents[1]
KERNEL = ROOT / "src/pulsara_agent/conversation_kernel"
PROVIDER_PRODUCTION_MODULES = (
    KERNEL / "assembler.py",
    KERNEL / "runner.py",
    KERNEL / "direct_model.py",
    ROOT / "src/pulsara_agent/llm/normalized_transport.py",
    ROOT / "src/pulsara_agent/llm/adapters/openai/events.py",
    ROOT / "src/pulsara_agent/llm/adapters/openai/responses.py",
    ROOT / "src/pulsara_agent/llm/adapters/openai/chat_completions.py",
    ROOT / "src/pulsara_agent/ports/provider_stream.py",
    ROOT / "src/pulsara_agent/ports/live_agent_event.py",
)


def _repository_aggregate_source() -> str:
    paths = [KERNEL / "repository.py"]
    paths.extend(sorted((KERNEL / "_repository").glob("*.py")))
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def test_stage2_registry_schema_and_removed_job_universe_are_exact() -> None:
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 29
    assert len(LIVE_EVENT_TYPES) == 24
    assert len(SUBJECT_SLOTS) == 11
    assert APPEND_GUARDS == ("HostWriterGuard",)
    assert len(CONVERSATION_KERNEL_RELATIONS) == 28

    policy = build_postgres_runtime_grant_policy()
    assert policy.relation_privileges == CONVERSATION_KERNEL_RUNTIME_PRIVILEGES
    assert build_postgres_schema_manifest().product_relations == (
        CONVERSATION_KERNEL_RELATIONS
    )


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
import pulsara_agent.terminal_protocol.v3_gateway

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
    repository = _repository_aggregate_source()
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
        / "src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql"
    ).read_text(encoding="utf-8")
    assert "stream_segment" not in migration.lower()
    assert "coalescing" not in migration.lower()
    assert "def complete_turn(" not in repository


def test_stage2_product_contract_survives_the_clean_migration_universe() -> None:
    report = json.loads(
        (
            ROOT
            / "benchmarks/suites/core/v1/durability_subtraction_stage2_activation.json"
        ).read_text(encoding="utf-8")
    )
    assert report["schema_version"] == "durability-subtraction-stage2-activation.v1"
    assert report["status"] in {"activation_candidate", "activated"}
    assert report["authority_activation"] == "single_reset_only"
    latest = POSTGRES_MIGRATION_REGISTRY.definition(0)
    manifest = build_postgres_schema_manifest()
    assert POSTGRES_MIGRATION_REGISTRY.latest_version == 0
    assert manifest.product_relations == CONVERSATION_KERNEL_RELATIONS
    assert latest.resource_name == "0000_conversation_kernel_baseline.sql"
    # This is immutable Stage 2 activation evidence, not a rolling current
    # product manifest.  Round 2 owns a separate 27-event activation record.
    assert report["vocabulary"] == {
        "committed": 26,
        "live": 23,
        "subject_slots": 13,
        "append_guards": 2,
    }
    assert report["protocol"] == {
        "major": 3,
        "minor": 0,
        "schema_fingerprint": (
            "sha256:30c3dec486dea592a4e43650006b1d547e4c44d993d68255941d77b61dd4a05c"
        ),
    }
    assert report["runtime_limits"] == {
        "contract": "stage2_runtime_limits.v1",
        "named_finite_fields": 62,
    }
    # Stage 2 evidence is immutable.  Round 5A removes the old Host-close and
    # model-call count/deadline admission caps; Round 5B removes the final
    # durable-job family, its four now-ownerless limits, and the independent
    # 128K provider-input cap.  The resolved model target now owns input budget.
    assert len(fields(Stage2RuntimeLimits)) == 55
    assert all(value > 0 for value in asdict(STAGE2_LIMITS).values())
    assert report["structural_budgets"] == {
        "contract": "stage2_structural_budgets.v1",
        **asdict(STAGE2_STRUCTURAL_BUDGETS),
    }


def test_stage2_ordinary_host_and_renderer_neutral_protocol_select_kernel_v3() -> None:
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from pulsara_agent.terminal_protocol import TerminalKernelProtocolServer

    assert KernelHostCore.__module__ == "pulsara_agent.conversation_kernel.host"
    assert TerminalKernelProtocolServer.__module__ == (
        "pulsara_agent.terminal_protocol.v3_gateway"
    )
    assert not (ROOT / "src/pulsara_agent/host").exists()
    assert not (ROOT / "src/pulsara_agent/terminal_client").exists()
    assert not (ROOT / "clients/terminal").exists()
    cli = (ROOT / "src/pulsara_agent/cli.py").read_text(encoding="utf-8")
    assert "result = asyncio.run(_kernel_host_run(args))" in cli
    assert "asyncio.run(_kernel_host_repl(args))" in cli
    assert "_kernel_host_tui" not in cli
    assert "_host_inspect" not in cli
    assert 'host_commands.add_parser("tui")' not in cli
    assert 'host_commands.add_parser("inspect")' not in cli
    gateway = (ROOT / "src/pulsara_agent/terminal_protocol/v3_gateway.py").read_text(
        encoding="utf-8"
    )
    assert "class TerminalKernelProtocolServer" in gateway
    assert "terminal_presentation" not in gateway


def test_stage2_protocol_v3_does_not_expose_durable_event_or_blob_identity() -> None:
    proto = (
        ROOT / "src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto"
    ).read_text(encoding="utf-8")
    assert "package pulsara.terminal.v3;" in proto
    assert "go_package" not in proto
    assert "StoredCommittedEvent" not in proto
    assert "RawStoredEvent" not in proto
    assert "string blob_id" not in proto
    assert "private_url" not in proto


def test_stage2_extension_and_tool_policy_have_single_production_owners() -> None:
    repository = _repository_aggregate_source()
    coordinators = tuple(
        KERNEL / relative
        for relative in (
            "runner.py",
            "provider_dispatch.py",
            "tool_execution.py",
            "turn_admission.py",
            "steer_consumption.py",
            "plan_runtime.py",
            "memory/dispatch.py",
            "compaction/coordinator.py",
        )
    )
    coordinator_source = "\n".join(
        path.read_text(encoding="utf-8") for path in coordinators
    )
    extensions = (KERNEL / "extensions.py").read_text(encoding="utf-8")
    tool_runtime = (KERNEL / "tool_runtime.py").read_text(encoding="utf-8")
    tool_policy = (KERNEL / "tool_policy.py").read_text(encoding="utf-8")

    assert "post_commit_tap" in repository
    assert "_finish_event_batch(committed=exc_type is None)" in repository
    assert "PostCommitHookOffer" not in coordinator_source
    assert "_offer_post_commit" not in coordinator_source
    assert "ConversationKernelRepository" not in extensions
    assert "ToolDispatchAuthorizationPolicy" in tool_runtime
    assert "class DefaultToolDispatchAuthorizationPolicy" in tool_policy
    assert "PolicyPermissionGate" not in tool_runtime


def test_stage2_runner_decomposition_has_exact_owners_and_import_direction() -> None:
    coordinator_relatives = (
        "provider_dispatch.py",
        "tool_execution.py",
        "turn_admission.py",
        "steer_consumption.py",
        "plan_runtime.py",
        "memory/dispatch.py",
        "compaction/coordinator.py",
    )
    coordinator_paths = tuple(KERNEL / relative for relative in coordinator_relatives)
    runner_path = KERNEL / "runner.py"
    runner_tree = ast.parse(
        runner_path.read_text(encoding="utf-8"), filename=str(runner_path)
    )
    runner_classes = {
        node.name: node for node in runner_tree.body if isinstance(node, ast.ClassDef)
    }
    tool_composition = runner_classes["_RunnerToolCompositionPort"]
    assert {ast.unparse(base) for base in tool_composition.bases} == {
        "Protocol",
        "ToolInvocationPort",
        "ToolSurfacePlanningPort",
    }
    runner_init = next(
        node
        for node in runner_classes["ConversationKernelRunner"].body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    runner_tools_arg = next(
        argument
        for argument in (*runner_init.args.args, *runner_init.args.kwonlyargs)
        if argument.arg == "tools"
    )
    assert ast.unparse(runner_tools_arg.annotation) == "_RunnerToolCompositionPort"

    def constructor_tool_annotation(path: Path, class_name: str) -> str:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        owner = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        constructor = next(
            node
            for node in owner.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        tools_argument = next(
            argument
            for argument in (
                *constructor.args.args,
                *constructor.args.kwonlyargs,
            )
            if argument.arg == "tools"
        )
        return ast.unparse(tools_argument.annotation)

    assert (
        constructor_tool_annotation(
            KERNEL / "provider_dispatch.py", "ProviderDispatchCoordinator"
        )
        == "ToolSurfacePlanningPort"
    )
    assert (
        constructor_tool_annotation(
            KERNEL / "compaction/coordinator.py", "CompactionCoordinator"
        )
        == "ToolSurfacePlanningPort"
    )
    assert (
        constructor_tool_annotation(KERNEL / "tool_execution.py", "ToolBatchExecutor")
        == "ToolInvocationPort"
    )
    runner_methods = {
        node.name
        for node in ast.walk(runner_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert runner_methods.isdisjoint(
        {
            "_accept_turn_exact",
            "_prepare_provider_dispatch",
            "_execute_compaction_fenced",
            "_accept_plan_control_batch",
            "_execute_tool_batch",
            "_settle_known_tool_result",
            "_apply_memory_sources",
            "_hydrate_pending_steers",
        }
    )
    runner_imports = {
        node.module
        for node in ast.walk(runner_tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert {
        "pulsara_agent.conversation_kernel.provider_dispatch",
        "pulsara_agent.conversation_kernel.tool_execution",
        "pulsara_agent.conversation_kernel.turn_admission",
        "pulsara_agent.conversation_kernel.plan_runtime",
        "pulsara_agent.conversation_kernel.memory.dispatch",
        "pulsara_agent.conversation_kernel.compaction.coordinator",
    } <= runner_imports

    imported_by_path: dict[str, set[str]] = {}
    for path in coordinator_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        imported_by_path[path.relative_to(KERNEL).as_posix()] = imported
        assert "pulsara_agent.conversation_kernel.runner" not in imported, path
        class_names = {
            node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        }
        assert not any(name.endswith("Mixin") for name in class_names), path
        assert "RunnerContext" not in class_names
        assert not any(
            isinstance(node, ast.Attribute) and node.attr == "_runner"
            for node in ast.walk(tree)
        ), path

    assert not any(
        imported == "pulsara_agent.conversation_kernel.provider_dispatch"
        or imported == "pulsara_agent.model_input.compiler"
        or imported == "pulsara_agent.conversation_kernel.host"
        for imported in imported_by_path["tool_execution.py"]
    )
    assert not any(
        imported == "pulsara_agent.conversation_kernel.host"
        or imported == "pulsara_agent.conversation_kernel.subagent"
        or imported.startswith("pulsara_agent.llm.adapters")
        for imported in imported_by_path["compaction/coordinator.py"]
    )

    tool_contracts_path = KERNEL / "tool_contracts.py"
    tool_contracts_tree = ast.parse(
        tool_contracts_path.read_text(encoding="utf-8"),
        filename=str(tool_contracts_path),
    )
    tool_contract_imports = {
        node.module
        for node in ast.walk(tool_contracts_tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(
        imported == "pulsara_agent.conversation_kernel.repository"
        or imported == "pulsara_agent.conversation_kernel.host"
        or imported == "pulsara_agent.conversation_kernel.runner"
        or imported == "pulsara_agent.conversation_kernel.tool_runtime"
        or imported.startswith("pulsara_agent.llm.adapters")
        for imported in tool_contract_imports
    )

    moved_tool_contracts = {
        "KernelToolResult",
        "KernelToolInvocationContext",
        "KernelToolAuthorization",
        "KernelToolAuthorizationKind",
        "KernelToolPhysicalInvocationError",
        "ProcessLocalEffectSettlementToken",
        "ProcessLocalEffectSettlementDisposition",
        "ProcessLocalEffectSettlementOutcome",
        "ProcessLocalEffectSettlementResult",
        "KernelToolLiveSink",
    }
    for relative in ("tool_runtime.py", "memory_tools.py", "subagent.py"):
        path = KERNEL / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        runner_imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "pulsara_agent.conversation_kernel.runner"
            for alias in node.names
        }
        assert runner_imported_names.isdisjoint(moved_tool_contracts), path


def test_stage2_cleanup_dead_symbols_and_compatibility_paths_cannot_return() -> None:
    assert not (ROOT / "src/pulsara_agent/tools/registry.py").exists()
    assert not (ROOT / "src/pulsara_agent/message/message.py").exists()
    removed_identifiers = {
        "ToolRegistry",
        "ToolRegistryReadPort",
        "ToolActionClassifierBinding",
        "ToolActionClassifierRegistry",
        "ToolActionClassifierContractError",
        "default_tool_action_classifier_registry",
        "builtin_tool_action_policy",
        "mcp_tool_action_policy",
        "ToolActionClassificationFact",
        "frozen_non_trigger_context_sources_identity_digest",
        "llm_context_fingerprint",
        "PresetPermissionPolicyFact",
        "preset_permission_policy_fact",
        "postgres_operation_deadline",
        "DIRECT_KERNEL_TOOL_NAMES",
        "PromptStatus",
        "MemoryQueryDisposition",
        "PlanInteractionStatus",
    }
    observed: set[str] = set()
    for path in sorted((ROOT / "src/pulsara_agent").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        observed.update(
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        )
        observed.update(
            node.name
            for node in ast.walk(tree)
            if isinstance(
                node,
                (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            )
        )
    assert observed.isdisjoint(removed_identifiers)


def test_stage2_provider_admission_and_blob_gc_are_physical_not_heuristic() -> None:
    direct = (KERNEL / "direct_model.py").read_text(encoding="utf-8")
    auxiliary = (KERNEL / "auxiliary_model.py").read_text(encoding="utf-8")
    reader = (KERNEL / "reader.py").read_text(encoding="utf-8")
    blob = (KERNEL / "blob.py").read_text(encoding="utf-8")
    host = (KERNEL / "host.py").read_text(encoding="utf-8")

    # Foreground model input is estimated by the pure structured compiler and
    # exact-joined to the transport-aware final validator. Auxiliary advisory
    # calls retain the semantic estimate for telemetry only: shape/binding is
    # validated separately, while admission uses the adapter's final-wire
    # materialization and the single local estimator.
    assert "validate_model_context_for_call" in direct
    assert "validated.estimate != compiled.final_estimate" in direct
    assert "estimate_model_context_for_call" not in direct
    assert "estimate_model_context_for_call" in auxiliary
    assert "validate_model_context_shape_for_call" in auxiliary
    assert "materialize_chat_context_bearing_wire_projection" in auxiliary
    assert "materialize_responses_context_bearing_wire_projection" in auxiliary
    assert "estimate_final_wire_json_components" in auxiliary
    assert "validate_model_context_for_call(" not in auxiliary
    for source in (direct, auxiliary):
        assert "canonical_bytes / 4" not in source
    assert "CanonicalProviderContinuityError" in reader
    assert "delete_orphans" in blob
    assert "kernel-blob-orphan-gc" in host
