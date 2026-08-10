"""Generate the read-only Stage 3-5 physical-deletion audit manifest.

The output is evidence, not a runtime registry.  It is deliberately derived
from the current working tree on every invocation so a later checkpoint cannot
silently reuse the import/reference counts captured by an earlier one.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import subprocess
import tarfile
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "pulsara_agent"
TEST_ROOT = ROOT / "tests"


@dataclass(frozen=True)
class Rule:
    prefix: str
    stage: int
    disposition: str
    owner: str
    successor: str
    retained_test: str


RULES = (
    Rule(
        "src/pulsara_agent/terminal_process/",
        3,
        "extract-neutral-leaf",
        "Host-scoped process-local terminal owner",
        "same neutral process owner",
        "tests/test_stage2_terminal_host_lifetime.py",
    ),
    Rule(
        "src/pulsara_agent/terminal_client/process_supervision.py",
        3,
        "extract-neutral-leaf",
        "Protocol v3 child/TTY supervision",
        "same v3-only supervision leaf",
        "tests/test_stage2_tui_cross_language.py",
    ),
    Rule(
        "src/pulsara_agent/mcp_config.py",
        3,
        "extract-neutral-leaf",
        "neutral MCP configuration detection",
        "fail-closed Kernel composition check",
        "tests/test_stage2_kernel_composition.py",
    ),
    Rule(
        "src/pulsara_agent/storage/postgres_connection_provider.py",
        4,
        "extract-neutral-leaf",
        "verified PostgreSQL connection lanes",
        "binding-v2 connection provider",
        "tests/test_stage5_clean_migration.py",
    ),
    Rule(
        "src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql",
        5,
        "retain",
        "clean migration-universe baseline",
        "same clean version-0 baseline",
        "tests/test_stage5_clean_migration.py",
    ),
    Rule(
        "src/pulsara_agent/storage/migrations/sql/__init__.py",
        5,
        "retain",
        "clean migration SQL resource package",
        "same sealed resource package",
        "tests/test_stage5_clean_migration.py",
    ),
    Rule(
        "src/pulsara_agent/runtime/authority_materialization/",
        3,
        "delete",
        "legacy derived-authority materialization owner",
        "direct pulsara_v3 canonical repository",
        "tests/test_stage2_conversation_kernel_postgres.py",
    ),
    Rule(
        "src/pulsara_agent/runtime/terminal_presentation/",
        3,
        "delete",
        "legacy Presentation Foundation owner",
        "Protocol v3 canonical snapshot/observation",
        "tests/test_stage2_protocol_v3.py",
    ),
    Rule(
        "src/pulsara_agent/runtime/context_input/audit_",
        3,
        "delete",
        "legacy exact context-audit owner",
        "best-effort operational diagnostics",
        "tests/test_stage2_conversation_runner.py",
    ),
    Rule(
        "src/pulsara_agent/runtime/projection_jobs/compaction_memory_driver.py",
        3,
        "delete",
        "legacy RuntimeSession compaction-memory driver",
        "minimal durable job kernel",
        "tests/test_stage2_job_executor.py",
    ),
    Rule(
        "src/pulsara_agent/runtime/projection_jobs/compaction_memory_settlement.py",
        3,
        "delete",
        "legacy RuntimeSession compaction-memory settlement",
        "minimal durable job kernel",
        "tests/test_stage2_job_executor.py",
    ),
    Rule(
        "src/pulsara_agent/runtime/projection_jobs/",
        4,
        "delete",
        "legacy projection-job runtime",
        "exact-four durable job kernel",
        "tests/test_stage2_job_executor.py",
    ),
    Rule(
        "src/pulsara_agent/projection_jobs/",
        4,
        "delete",
        "legacy projection-job contracts",
        "exact-four durable job kernel",
        "tests/test_stage2_job_executor.py",
    ),
    Rule(
        "src/pulsara_agent/event_log/",
        5,
        "delete",
        "legacy universal EventLog",
        "conversation-kernel selective journal",
        "tests/test_stage2_conversation_kernel_postgres.py",
    ),
    Rule(
        "src/pulsara_agent/event/",
        5,
        "delete",
        "legacy 151-type event grammar",
        "exact 26 committed event vocabulary",
        "tests/test_stage2_architecture.py",
    ),
    Rule(
        "src/pulsara_agent/replay/",
        5,
        "delete",
        "legacy execution replay graph",
        "canonical conversation rehydrate",
        "tests/test_stage2_canonical_reader.py",
    ),
    Rule(
        "src/pulsara_agent/runtime/session.py",
        3,
        "delete",
        "legacy RuntimeSession",
        "KernelHostSession",
        "tests/test_stage2_kernel_composition.py",
    ),
    Rule(
        "src/pulsara_agent/runtime/model_",
        3,
        "delete",
        "legacy foreground model recovery",
        "process-local provider assembler",
        "tests/test_stage2_provider_stream.py",
    ),
    Rule(
        "src/pulsara_agent/runtime/committed_reducer_",
        3,
        "delete",
        "legacy committed reducer repair/post-fold",
        "direct canonical transaction",
        "tests/test_stage2_conversation_kernel_postgres.py",
    ),
    Rule(
        "src/pulsara_agent/runtime/projection_checkpoint_maintenance.py",
        3,
        "delete",
        "legacy projection checkpoint owner",
        "canonical rehydrate",
        "tests/test_stage2_canonical_reader.py",
    ),
    Rule(
        "src/pulsara_agent/llm/segment.py",
        3,
        "delete",
        "legacy durable stream segment",
        "process-local typed LiveAgentEvent assembler",
        "tests/test_stage2_provider_stream.py",
    ),
    Rule(
        "src/pulsara_agent/llm/raw_provider.py",
        3,
        "delete",
        "legacy raw provider carrier",
        "normalized provider transport",
        "tests/test_stage2_provider_stream.py",
    ),
    Rule(
        "src/pulsara_agent/llm/drafts.py",
        3,
        "delete",
        "legacy adoption/draft bridge",
        "CompletedAssistantMessageDraft",
        "tests/test_stage2_provider_stream.py",
    ),
    Rule(
        "src/pulsara_agent/llm/sanitizing_transport.py",
        3,
        "delete",
        "legacy duplicate transport bridge",
        "normalized provider transport",
        "tests/test_stage2_direct_model.py",
    ),
    Rule(
        "src/pulsara_agent/terminal_protocol/gateway.py",
        3,
        "delete",
        "Protocol v2 gateway",
        "Protocol v3 gateway",
        "tests/test_stage2_protocol_v3.py",
    ),
    Rule(
        "src/pulsara_agent/terminal_protocol/generated/",
        3,
        "delete",
        "Protocol v2 generated carriers",
        "Protocol v3 generated carriers",
        "tests/test_stage2_tui_cross_language.py",
    ),
    Rule(
        "src/pulsara_agent/terminal_protocol/schema/terminal_client.proto",
        3,
        "delete",
        "Protocol v2 wire schema",
        "terminal_kernel_v3.proto",
        "tests/test_stage2_tui_cross_language.py",
    ),
    Rule(
        "src/pulsara_agent/storage/runtime_write_admission.py",
        5,
        "delete",
        "legacy generic runtime-write admission",
        "HostWriterGuard and JobAttemptClaimGuard",
        "tests/test_stage2_conversation_kernel_postgres.py",
    ),
    Rule(
        "src/pulsara_agent/graph/",
        4,
        "delete",
        "legacy Oxigraph/RDF surface",
        "PostgreSQL memory facts/relations",
        "tests/test_stage2_job_executor.py",
    ),
    Rule(
        "clients/terminal/internal/presentation/",
        3,
        "delete",
        "Protocol v2 root-indexed client presentation",
        "kernelapp Protocol v3 sequence state",
        "clients/terminal/internal/kernelapp/model_test.go",
    ),
    Rule(
        "clients/terminal/internal/protocolvalue/",
        3,
        "delete",
        "Protocol v2 value carriers",
        "protocolv3 generated carriers",
        "clients/terminal/internal/kernelapp/model_test.go",
    ),
    Rule(
        "clients/terminal/internal/protocol/",
        3,
        "delete",
        "Protocol v2 client codec",
        "protocolv3 generated carriers",
        "clients/terminal/internal/kernelapp/model_test.go",
    ),
    Rule(
        "src/pulsara_agent/runtime/terminal/manager.py",
        3,
        "extract-neutral-leaf",
        "Host-owned process-local terminal manager",
        "neutral terminal process manager",
        "tests/test_stage2_terminal_host_lifetime.py",
    ),
    Rule(
        "src/pulsara_agent/runtime/terminal/process.py",
        3,
        "extract-neutral-leaf",
        "Host-owned process-local process registry",
        "neutral terminal process registry",
        "tests/test_stage2_terminal_host_lifetime.py",
    ),
    Rule(
        "src/pulsara_agent/terminal_client/launcher.py",
        3,
        "extract-neutral-leaf",
        "legacy launcher supervision helper",
        "v3-only process/TTY supervision",
        "tests/test_stage2_tui_cross_language.py",
    ),
    Rule(
        "src/pulsara_agent/storage/migrations/sql/",
        5,
        "delete",
        "legacy migration universe",
        "0000 conversation-kernel clean baseline",
        "tests/test_stage2_conversation_kernel_postgres.py",
    ),
)


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _module_for(path: Path) -> str | None:
    try:
        relative = path.relative_to(SOURCE_ROOT)
    except ValueError:
        return None
    if path.suffix != ".py":
        return None
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(("pulsara_agent", *parts))


def _imports(path: Path) -> set[str]:
    if path.suffix != ".py":
        return set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def _candidate_files() -> list[Path]:
    roots = (ROOT / "src", ROOT / "tests", ROOT / "tools", ROOT / "clients")
    return sorted(
        path
        for base in roots
        if base.exists()
        for path in base.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".go", ".proto", ".sql", ".json", ".toml"}
        and "/bin/" not in path.as_posix()
    )


def _head_tree() -> dict[str, bytes]:
    archive = subprocess.run(
        ["git", "archive", "HEAD", "src", "tests", "tools", "clients"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    result: dict[str, bytes] = {}
    with tarfile.open(fileobj=BytesIO(archive), mode="r:") as bundle:
        for member in bundle.getmembers():
            if not member.isfile():
                continue
            extracted = bundle.extractfile(member)
            if extracted is not None:
                result[member.name] = extracted.read()
    return result


def _deleted_paths() -> tuple[str, ...]:
    output = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=D", "HEAD", "--"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return tuple(sorted(line for line in output.splitlines() if line))


def _fallback_deleted_rule(relative: str) -> Rule:
    lower = relative.lower()
    if relative.startswith("clients/terminal/"):
        return Rule(
            relative,
            3,
            "delete",
            "Protocol v2 Go client surface",
            "Protocol v3 kernel client",
            "clients/terminal/internal/kernelapp/model_test.go",
        )
    if relative.startswith("tests/"):
        stage4_terms = (
            "projection",
            "oxigraph",
            "graph",
            "memory",
            "recall",
            "retrieval",
            "working_context",
            "governance",
        )
        stage5_terms = (
            "event_log",
            "event_message",
            "runtime_event",
            "schema_migration",
            "postgres_runtime_schema",
            "stage0",
            "recovery.py",
        )
        stage = (
            5
            if any(term in lower for term in stage5_terms)
            else 4
            if any(term in lower for term in stage4_terms)
            else 3
        )
        successor = {
            3: "canonical Kernel/Protocol v3 retained behavior suite",
            4: "exact-four jobs and PostgreSQL memory retained suite",
            5: "selective journal and clean migration-universe suite",
        }[stage]
        retained = {
            3: "tests/test_stage2_kernel_host_dogfood.py",
            4: "tests/test_stage2_conversation_kernel_postgres.py",
            5: "tests/test_stage5_clean_migration.py",
        }[stage]
        return Rule(
            relative,
            stage,
            "delete",
            "obsolete legacy-authority test or fixture",
            successor,
            retained,
        )
    if relative.startswith("src/pulsara_agent/"):
        stage5_prefixes = (
            "src/pulsara_agent/event/",
            "src/pulsara_agent/event_log/",
            "src/pulsara_agent/replay/",
            "src/pulsara_agent/storage/migrations/",
            "src/pulsara_agent/storage/runtime_write_admission.py",
            "src/pulsara_agent/storage/session_bootstrap.py",
        )
        stage4_prefixes = (
            "src/pulsara_agent/projection_jobs/",
            "src/pulsara_agent/graph/",
            "src/pulsara_agent/jsonld/",
            "src/pulsara_agent/ontology/",
            "src/pulsara_agent/memory/",
            "src/pulsara_agent/retrieval/",
            "src/pulsara_agent/storage/postgres_memory_projection.py",
            "src/pulsara_agent/storage/projection_migration_transaction.py",
        )
        stage = (
            5
            if relative.startswith(stage5_prefixes)
            else 4
            if relative.startswith(stage4_prefixes)
            else 3
        )
        successor = {
            3: "canonical conversation Kernel or neutral process leaf",
            4: "exact-four jobs and PostgreSQL relational memory",
            5: "selective journal and clean migration universe",
        }[stage]
        retained = {
            3: "tests/test_stage2_kernel_host_dogfood.py",
            4: "tests/test_stage2_job_executor.py",
            5: "tests/test_stage5_clean_migration.py",
        }[stage]
        return Rule(
            relative,
            stage,
            "delete",
            "legacy production authority or compatibility surface",
            successor,
            retained,
        )
    stage = 5 if relative.startswith("tools/") else 3
    return Rule(
        relative,
        stage,
        "delete",
        "legacy build or audit surface",
        "final Stage 3-5 architecture gates",
        "tests/test_stage3_5_architecture.py",
    )


def _rule_for(relative: str) -> Rule | None:
    matches = [rule for rule in RULES if relative.startswith(rule.prefix)]
    if not matches:
        return None
    return max(matches, key=lambda item: len(item.prefix))


def _targets() -> Iterable[tuple[Path, Rule]]:
    for path in _candidate_files():
        relative = path.relative_to(ROOT).as_posix()
        rule = _rule_for(relative)
        if rule is not None:
            yield path, rule


def _tree_fingerprint(paths: Iterable[Path]) -> str:
    digest = sha256()
    for path in sorted(paths):
        relative = path.relative_to(ROOT).as_posix().encode()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(sha256(path.read_bytes()).digest())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _module_for_relative(relative: str) -> str | None:
    path = ROOT / relative
    return _module_for(path)


def _importers_for_module(
    module: str | None,
    imports: dict[str, set[str]],
    *,
    exclude: str,
) -> list[str]:
    if module is None:
        return []
    return sorted(
        consumer
        for consumer, observed in imports.items()
        if consumer != exclude
        and any(name == module or name.startswith(module + ".") for name in observed)
    )


def _imports_from_bytes(relative: str, source: bytes) -> set[str]:
    if not relative.endswith(".py"):
        return set()
    try:
        tree = ast.parse(source.decode("utf-8"), filename=relative)
    except (SyntaxError, UnicodeDecodeError):
        return set()
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def build_manifest(checkpoint: str) -> dict[str, object]:
    files = _candidate_files()
    imports = {
        path.relative_to(ROOT).as_posix(): _imports(path)
        for path in files
        if path.suffix == ".py"
    }
    head_tree = _head_tree()
    historical_imports = {
        relative: _imports_from_bytes(relative, source)
        for relative, source in head_tree.items()
        if relative.endswith(".py")
    }
    rows: list[dict[str, object]] = []
    for path, rule in _targets():
        relative = path.relative_to(ROOT).as_posix()
        module = _module_for(path)
        importers = _importers_for_module(module, imports, exclude=relative)
        historical_importers = _importers_for_module(
            module, historical_imports, exclude=relative
        )
        source = path.read_text(encoding="utf-8", errors="replace")
        text_references = 0
        basename = path.name
        if basename and basename != "__init__.py":
            text_references = sum(
                other.read_text(encoding="utf-8", errors="ignore").count(basename)
                for other in files
                if other != path
            )
        rows.append(
            {
                "path": relative,
                "exists_in_worktree": True,
                "module": module,
                "planned_stage": rule.stage,
                "disposition": rule.disposition,
                "owner": rule.owner,
                "owner_close_await_count": source.count("await "),
                "production_importers": sorted(
                    item for item in importers if item.startswith("src/")
                ),
                "test_importers": sorted(
                    item for item in importers if item.startswith("tests/")
                ),
                "historical_production_last_consumers": sorted(
                    item for item in historical_importers if item.startswith("src/")
                ),
                "historical_test_last_consumers": sorted(
                    item for item in historical_importers if item.startswith("tests/")
                ),
                "cli_surface": "cli" in source.lower()
                or any(item.endswith("/cli.py") for item in importers),
                "schema_surface": path.suffix in {".sql", ".proto"}
                or "CREATE TABLE" in source,
                "config_surface": "settings" in source.lower()
                or "environment" in source.lower(),
                "successor": rule.successor,
                "retained_behavior_test": rule.retained_test,
                "remaining_import_reference_count": len(importers),
                "remaining_text_reference_count": text_references,
            }
        )
    current_relatives = {path.relative_to(ROOT).as_posix() for path in files}
    for relative in _deleted_paths():
        if relative in current_relatives:
            continue
        rule = _rule_for(relative) or _fallback_deleted_rule(relative)
        module = _module_for_relative(relative)
        importers = _importers_for_module(module, imports, exclude=relative)
        historical_importers = _importers_for_module(
            module, historical_imports, exclude=relative
        )
        historical_source = head_tree.get(relative, b"")
        basename = Path(relative).name
        text_references = (
            sum(
                path.read_text(encoding="utf-8", errors="ignore").count(basename)
                for path in files
            )
            if basename and basename != "__init__.py"
            else 0
        )
        rows.append(
            {
                "path": relative,
                "exists_in_worktree": False,
                "module": module,
                "planned_stage": rule.stage,
                "disposition": rule.disposition,
                "owner": rule.owner,
                "owner_close_await_count": historical_source.count(b"await "),
                "production_importers": sorted(
                    item for item in importers if item.startswith("src/")
                ),
                "test_importers": sorted(
                    item for item in importers if item.startswith("tests/")
                ),
                "historical_production_last_consumers": sorted(
                    item for item in historical_importers if item.startswith("src/")
                ),
                "historical_test_last_consumers": sorted(
                    item for item in historical_importers if item.startswith("tests/")
                ),
                "cli_surface": b"cli" in historical_source.lower(),
                "schema_surface": relative.endswith((".sql", ".proto"))
                or b"CREATE TABLE" in historical_source,
                "config_surface": b"settings" in historical_source.lower()
                or b"environment" in historical_source.lower(),
                "successor": rule.successor,
                "retained_behavior_test": rule.retained_test,
                "remaining_import_reference_count": len(importers),
                "remaining_text_reference_count": text_references,
                "historical_source_sha256": sha256(historical_source).hexdigest(),
            }
        )
    rows.sort(key=lambda item: str(item["path"]))
    remaining_rows = [row for row in rows if row["exists_in_worktree"]]
    completed_rows = [row for row in rows if not row["exists_in_worktree"]]
    return {
        "schema_version": "pulsara.durability-subtraction.deletion-manifest.v1",
        "checkpoint": checkpoint,
        "snapshot_semantics": (
            "read-only rescan of the named current worktree; this evidence does "
            "not reconstruct an earlier intermediate filesystem"
        ),
        "generated_at": datetime.now(UTC).isoformat(),
        "git_head": _git_head(),
        "working_tree_fingerprint": _tree_fingerprint(files),
        "target_count": len(rows),
        "remaining_target_count": len(remaining_rows),
        "physically_deleted_target_count": len(completed_rows),
        "unclassified_count": 0,
        "remaining_by_stage": {
            str(stage): sum(row["planned_stage"] == stage for row in remaining_rows)
            for stage in (3, 4, 5)
        },
        "physically_deleted_by_stage": {
            str(stage): sum(row["planned_stage"] == stage for row in completed_rows)
            for stage in (3, 4, 5)
        },
        "remaining_by_disposition": {
            disposition: sum(
                row["disposition"] == disposition for row in remaining_rows
            )
            for disposition in ("delete", "extract-neutral-leaf", "retain")
        },
        "targets": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    output = arguments.output
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(build_manifest(arguments.checkpoint), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
