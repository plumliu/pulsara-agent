"""Read-only Stage 0 durability inventory validator.

This tool deliberately parses production source instead of importing Runtime
modules.  It never rewrites the checked-in fixtures and never opens a database.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures"
MANIFEST_PATH = FIXTURE_ROOT / "durability_subtraction_stage0_manifest.json"
TARGET_PATH = FIXTURE_ROOT / "durability_subtraction_stage0_target_oracle.json"
OWNER_PATH = FIXTURE_ROOT / "durability_subtraction_stage0_owner_inventory.json"
FAULT_PATH = FIXTURE_ROOT / "durability_subtraction_stage0_fault_matrix.json"
BASELINE_PATH = FIXTURE_ROOT / "durability_subtraction_stage0_baseline.json"
ARCHITECTURE_PATH = (
    FIXTURE_ROOT / "durability_subtraction_stage0_architecture_baseline.json"
)

EXPECTED_CLASS_COUNTS = {"A": 39, "B": 25, "C": 16, "D": 71}
EXPECTED_TARGET_COUNTS = {
    "committed_core": 26,
    "live_core": 23,
    "subject_slots": 13,
    "append_guards": 2,
}
EXPECTED_STATIC_METRICS = {
    "schema_registry_version": 11,
    "sql_create_table_count": 62,
    "foreground_committed_reducers": 9,
    "mainline_reconciliation_latches": 6,
    "host_close_await_expressions": 45,
    "host_close_committed_reducer_barriers": 4,
    "non_host_runtime_teardown_await_expressions": 11,
}
OWNER_CLASSIFICATIONS = {
    "canonical_write",
    "semantic_consumer_no_fallback",
    "derived_acceleration",
    "presentation_observer",
    "operational_observer",
    "physical_owner_only",
}
REDUCER_SEMANTICS = {"semantic_required", "derived_best_effort"}
REQUIRED_OWNER_SURFACES = {
    "publisher_subscriber",
    "committed_reducer",
    "checkpoint",
    "audit",
    "presentation",
    "search_surface",
    "close_owner",
}
LIFECYCLE_FIELDS = {
    "producer_symbols",
    "consumer_symbols",
    "write_owner",
    "transaction_boundary",
    "durability",
    "sensitivity",
    "recovery_or_gate_role",
    "removal_stage",
    "evidence_paths",
}
TRACKED_ARCHITECTURE_SYMBOLS = (
    "PreparedContextInputAuditSourceCapture",
    "bind_optional_context_audit_source",
    "offer_best_effort_nowait",
    "latch_publication_reconciliation_required",
    "RuntimeProjectionCheckpointAdmissionBlocked",
    "await_committed_reducer_repair_safe_point",
    "drain_open_committed_reducer_barrier",
    "stop_admission_and_drain",
    "drain_pending",
    "close_if_idle",
    "create_task",
    "shield",
    "gather",
    "wait_for",
    "to_thread",
    "run_in_executor",
)
DERIVED_UI_PREFIXES = (
    "src/pulsara_agent/runtime/terminal_presentation/",
    "src/pulsara_agent/runtime/terminal/observation.py",
    "src/pulsara_agent/runtime/terminal/ui_stream.py",
)
FORBIDDEN_DERIVED_UI_SYMBOLS = {
    "require_mutation_allowed",
    "latch_publication_reconciliation_required",
    "CommittedEventSettlementReceipt",
}
STAGE1_REMOVED_ARCHITECTURE_SYMBOLS = {
    "PreparedContextInputAuditSourceCapture",
    "bind_optional_context_audit_source",
    "offer_best_effort_nowait",
}


class InventoryError(RuntimeError):
    """Raised when the checked-in Stage 0 evidence drifts."""


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InventoryError(f"{path.relative_to(ROOT)} must contain an object")
    return value


def _event_type_names() -> list[str]:
    path = ROOT / "src" / "pulsara_agent" / "event" / "events.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "EventType":
            names: list[str] = []
            for child in node.body:
                if (
                    isinstance(child, ast.Assign)
                    and len(child.targets) == 1
                    and isinstance(child.targets[0], ast.Name)
                ):
                    names.append(child.targets[0].id)
                elif isinstance(child, ast.AnnAssign) and isinstance(
                    child.target, ast.Name
                ):
                    names.append(child.target.id)
            return names
    raise InventoryError("EventType AST definition is missing")


def _materialized_events(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    defaults = manifest.get("family_defaults")
    rows = manifest.get("events")
    if not isinstance(defaults, dict) or not isinstance(rows, list):
        raise InventoryError("lifecycle manifest shape is invalid")
    resolved: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise InventoryError("lifecycle manifest event must be an object")
        family = row.get("family")
        inherited = defaults.get(family)
        if not isinstance(inherited, dict):
            raise InventoryError(f"unknown lifecycle family: {family!r}")
        materialized = {**inherited, **row}
        missing = LIFECYCLE_FIELDS - materialized.keys()
        if missing:
            raise InventoryError(
                f"{row.get('current_type')} misses lifecycle fields: {sorted(missing)}"
            )
        for evidence in materialized["evidence_paths"]:
            if Path(evidence).is_absolute() or not (ROOT / evidence).exists():
                raise InventoryError(
                    f"invalid evidence path for {row.get('current_type')}: {evidence}"
                )
        resolved.append(materialized)
    return resolved


def _target_union(rows: Iterable[dict[str, Any]], target_key: str) -> set[str]:
    result: set[str] = set()
    for row in rows:
        target = row.get("target_core_type")
        if target is None:
            continue
        if not isinstance(target, dict):
            raise InventoryError("target_core_type must be null or an object")
        values = target.get(target_key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise InventoryError(f"target_core_type.{target_key} is invalid")
        result.update(values)
    return result


def _qualname_stack(tree: ast.AST) -> dict[ast.AST, str]:
    names: dict[ast.AST, str] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.stack.append(node.name)
            names[node] = ".".join(self.stack)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            names[node] = ".".join(self.stack)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def generic_visit(self, node: ast.AST) -> None:
            names[node] = ".".join(self.stack) or "<module>"
            super().generic_visit(node)

    Visitor().visit(tree)
    return names


def _symbol_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def architecture_observations() -> dict[str, list[dict[str, Any]]]:
    raw: dict[str, Counter[tuple[str, str, str]]] = {
        name: Counter() for name in TRACKED_ARCHITECTURE_SYMBOLS
    }
    source_root = ROOT / "src" / "pulsara_agent"
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        qualnames = _qualname_stack(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _symbol_name(node.func)
            if name not in raw:
                continue
            raw[name][(relative, qualnames.get(node, "<module>"), name)] += 1
    observations: dict[str, list[dict[str, Any]]] = {}
    for name, counts in raw.items():
        observations[name] = [
            {"path": path, "owner": owner, "call": call, "count": count}
            for (path, owner, call), count in sorted(counts.items())
        ]
    return observations


def _tree_digest(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        relative = path.relative_to(ROOT).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def production_surface_digests() -> dict[str, str]:
    migration_root = ROOT / "src" / "pulsara_agent" / "storage" / "migrations"
    protocol_root = ROOT / "src" / "pulsara_agent" / "terminal_protocol" / "schema"
    schema_paths = [
        path
        for path in migration_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]
    schema_paths.extend(
        path
        for path in protocol_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    python_paths = sorted((ROOT / "src" / "pulsara_agent").rglob("*.py"))
    import_rows: list[str] = []
    for path in python_paths:
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    import_rows.append(f"{relative}|import|{alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    import_rows.append(
                        f"{relative}|from|{node.level}|{module}|{alias.name}"
                    )
    import_payload = "\n".join(sorted(import_rows)).encode("utf-8")
    return {
        "schema_and_protocol": _tree_digest(schema_paths),
        "production_import_graph": f"sha256:{hashlib.sha256(import_payload).hexdigest()}",
    }


def _class_method(
    relative: str, class_name: str, method_name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    path = ROOT / relative
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                child.name == method_name
            ):
                return child
    raise InventoryError(f"missing method: {class_name}.{method_name}")


def _call_count(node: ast.AST, symbol: str) -> int:
    return sum(
        isinstance(child, ast.Call) and _symbol_name(child.func) == symbol
        for child in ast.walk(node)
    )


def static_baseline_metrics() -> dict[str, int]:
    serialization_path = (
        ROOT / "src" / "pulsara_agent" / "event_log" / "serialization.py"
    )
    serialization_tree = ast.parse(
        serialization_path.read_text(encoding="utf-8"),
        filename=str(serialization_path.relative_to(ROOT)),
    )
    schema_registry_version: int | None = None
    for node in serialization_tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id == "AGENT_EVENT_SCHEMA_VERSION"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, int)
        ):
            schema_registry_version = node.value.value
            break
    if schema_registry_version is None:
        raise InventoryError("AGENT_EVENT_SCHEMA_VERSION is not a literal integer")

    migration_root = ROOT / "src" / "pulsara_agent" / "storage" / "migrations"
    sql_create_table_count = sum(
        len(re.findall(r"\bCREATE\s+TABLE\b", path.read_text(encoding="utf-8"), re.I))
        for path in sorted(migration_root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    )

    runtime_session_path = ROOT / "src" / "pulsara_agent" / "runtime" / "session.py"
    runtime_session_tree = ast.parse(
        runtime_session_path.read_text(encoding="utf-8"),
        filename=str(runtime_session_path.relative_to(ROOT)),
    )
    foreground_committed_reducers = _call_count(
        runtime_session_tree, "register_committed_reducer"
    )
    latch_names = {
        "_reconciliation_required",
        "_ledger_reconciliation_required",
        "_context_input_reconciliation_required",
        "_memory_governance_reconciliation_required",
        "_publication_reconciliation_required",
        "_mandatory_audit_reconciliation_required",
    }
    declared_latches = {
        child.target.id
        for child in ast.walk(runtime_session_tree)
        if isinstance(child, ast.AnnAssign)
        and isinstance(child.target, ast.Name)
        and child.target.id in latch_names
    }
    if declared_latches != latch_names:
        raise InventoryError("mainline reconciliation latch declarations drifted")

    host_close = _class_method(
        "src/pulsara_agent/host/session.py", "HostSession", "aclose"
    )
    non_host_teardown = _class_method(
        "src/pulsara_agent/runtime/session.py",
        "RuntimeSession",
        "teardown_non_host_runtime_session",
    )
    return {
        "schema_registry_version": schema_registry_version,
        "sql_create_table_count": sql_create_table_count,
        "foreground_committed_reducers": foreground_committed_reducers,
        "mainline_reconciliation_latches": len(declared_latches),
        "host_close_await_expressions": sum(
            isinstance(child, ast.Await) for child in ast.walk(host_close)
        ),
        "host_close_committed_reducer_barriers": _call_count(
            host_close, "drain_open_committed_reducer_barrier"
        ),
        "non_host_runtime_teardown_await_expressions": sum(
            isinstance(child, ast.Await) for child in ast.walk(non_host_teardown)
        ),
    }


def validate(*, allow_stage2_schema_addition: bool = False) -> dict[str, Any]:
    manifest = _load(MANIFEST_PATH)
    target = _load(TARGET_PATH)
    owners = _load(OWNER_PATH)
    faults = _load(FAULT_PATH)
    baseline = _load(BASELINE_PATH)
    architecture = _load(ARCHITECTURE_PATH)

    rows = _materialized_events(manifest)
    actual_names = _event_type_names()
    manifest_names = [row.get("current_type") for row in rows]
    if manifest_names != actual_names:
        raise InventoryError("lifecycle manifest does not exactly match EventType AST")
    if len(set(manifest_names)) != 151:
        raise InventoryError("lifecycle manifest must contain 151 unique EventTypes")
    counts = Counter(row.get("target_class") for row in rows)
    if dict(counts) != EXPECTED_CLASS_COUNTS:
        raise InventoryError(f"lifecycle class count drifted: {dict(counts)}")

    committed = _target_union(rows, "committed")
    live = _target_union(rows, "live")
    committed_oracle = target.get("committed_core")
    live_oracle = target.get("live_core")
    if not isinstance(committed_oracle, list) or not all(
        isinstance(item, dict) and isinstance(item.get("type"), str)
        for item in committed_oracle
    ):
        raise InventoryError("committed target oracle is invalid")
    if not isinstance(live_oracle, list) or not all(
        isinstance(item, str) for item in live_oracle
    ):
        raise InventoryError("live target oracle is invalid")
    expected_committed = {item["type"] for item in committed_oracle}
    expected_live = set(live_oracle)
    if committed != expected_committed or live != expected_live:
        raise InventoryError("manifest semantic target union drifted from oracle")
    for key, expected in EXPECTED_TARGET_COUNTS.items():
        value = target.get(key)
        if not isinstance(value, list) or len(value) != expected:
            raise InventoryError(f"target oracle {key} must contain {expected} entries")
    subject_slots = set(target["subject_slots"])
    append_guards = set(target["append_guards"])
    for item in committed_oracle:
        if item.get("subject_slot") not in subject_slots:
            raise InventoryError(
                f"committed target has unknown subject slot: {item['type']}"
            )
        item_guards = item.get("append_guards")
        if not isinstance(item_guards, list) or not item_guards:
            raise InventoryError(
                f"committed target has no append guard: {item['type']}"
            )
        if not set(item_guards) <= append_guards:
            raise InventoryError(
                f"committed target has unknown append guard: {item['type']}"
            )
    formal_targets = committed | live
    forbidden = set(target.get("forbidden_formal_targets", []))
    if formal_targets & forbidden:
        raise InventoryError("forbidden target type entered formal target oracle")
    if any(name.startswith("RawProvider") for name in formal_targets):
        raise InventoryError("RawProvider target entered formal target oracle")

    owner_rows = owners.get("owners")
    if not isinstance(owner_rows, list) or not owner_rows:
        raise InventoryError("owner inventory is empty")
    owner_ids: set[str] = set()
    observed_surfaces: set[str] = set()
    required_owner_fields = {
        "owner_id",
        "symbol",
        "surface",
        "producer_input",
        "durable_input_truth",
        "output",
        "output_rebuildable",
        "failure_current_propagation",
        "enters_mutation_gate",
        "enters_run_completion",
        "enters_host_close",
        "enters_non_host_close",
        "resources",
        "stop_admission",
        "cancel_or_terminate",
        "physical_join",
        "stage1_classification",
        "reducer_semantics",
        "bounded_fallback",
        "evidence_paths",
    }
    for row in owner_rows:
        missing = required_owner_fields - row.keys()
        if missing:
            raise InventoryError(
                f"owner {row.get('owner_id')} misses fields: {sorted(missing)}"
            )
        owner_id = row["owner_id"]
        if owner_id in owner_ids:
            raise InventoryError(f"duplicate owner inventory id: {owner_id}")
        owner_ids.add(owner_id)
        observed_surfaces.add(row["surface"])
        if row["stage1_classification"] not in OWNER_CLASSIFICATIONS:
            raise InventoryError(f"invalid owner classification: {owner_id}")
        semantics = row["reducer_semantics"]
        if semantics is not None and semantics not in REDUCER_SEMANTICS:
            raise InventoryError(f"invalid reducer semantics: {owner_id}")
        if semantics == "derived_best_effort" and not row["bounded_fallback"]:
            raise InventoryError(
                f"derived reducer lacks bounded fallback evidence: {owner_id}"
            )
        if row["stage1_classification"] == "semantic_consumer_no_fallback":
            blocker = row.get("stage1_blocker")
            minimum = row.get("minimum_fallback")
            if not blocker and not minimum:
                raise InventoryError(
                    f"semantic consumer lacks explicit blocker/fallback: {owner_id}"
                )
        for evidence in row["evidence_paths"]:
            if Path(evidence).is_absolute() or not (ROOT / evidence).exists():
                raise InventoryError(f"invalid owner evidence path: {evidence}")
    if not REQUIRED_OWNER_SURFACES <= observed_surfaces:
        raise InventoryError(
            f"owner surfaces are incomplete: {sorted(REQUIRED_OWNER_SURFACES - observed_surfaces)}"
        )

    scenarios = faults.get("scenarios")
    required_faults = set(faults.get("required_scenario_ids", []))
    if (
        not isinstance(scenarios, list)
        or {item.get("scenario_id") for item in scenarios} != required_faults
    ):
        raise InventoryError("fault characterization scenarios are incomplete")
    for scenario in scenarios:
        if scenario.get("failure_class") not in {"semantic", "derived", "physical"}:
            raise InventoryError("fault scenario failure class is invalid")
        if not scenario.get("test_ids"):
            raise InventoryError("fault scenario lacks a stable test entry")

    serialized_baseline = json.dumps(baseline, ensure_ascii=False)
    if "/Users/" in serialized_baseline or "postgresql://" in serialized_baseline:
        raise InventoryError("baseline fixture contains environment-specific data")
    observed_static_metrics = static_baseline_metrics()
    expected_static_metrics = dict(EXPECTED_STATIC_METRICS)
    if allow_stage2_schema_addition:
        # Stage 0 remains a frozen observation.  Stage 2 adds exactly its 24
        # active product relations in one new migration; it must not silently
        # refresh the historical baseline or relax any other metric.
        expected_static_metrics["sql_create_table_count"] += 24
    if observed_static_metrics != expected_static_metrics:
        raise InventoryError(
            f"Stage 0 frozen static metrics drifted: {observed_static_metrics}"
        )
    empirical_metrics = baseline.get("frozen_empirical_metrics")
    expected_empirical_metrics = {
        "text_only_universal_events": 43,
        "one_tool_universal_events": 83,
        "text_steady_state_durable_write_scope_minimum": 15,
        "one_tool_steady_state_durable_write_scope_minimum": 31,
        "normal_model_call_audit_artifacts": 4,
    }
    if empirical_metrics != expected_empirical_metrics:
        raise InventoryError("Stage 0 frozen empirical metrics drifted")

    observed_architecture = architecture_observations()
    expected_architecture = architecture.get("symbol_calls")
    for symbol, expected_calls in expected_architecture.items():
        observed_calls = observed_architecture[symbol]
        if symbol in STAGE1_REMOVED_ARCHITECTURE_SYMBOLS:
            if observed_calls:
                raise InventoryError(
                    f"Stage 1 automatic-audit symbol remains reachable: {symbol}"
                )
            if not expected_calls:
                raise InventoryError(f"Stage 0 baseline lacks removed symbol: {symbol}")
        elif allow_stage2_schema_addition:
            observed_by_identity = {
                (row["path"], row["owner"], row["call"]): row["count"]
                for row in observed_calls
            }
            for row in expected_calls:
                identity = (row["path"], row["owner"], row["call"])
                if observed_by_identity.get(identity, 0) < row["count"]:
                    raise InventoryError(
                        f"Stage 2 removed retained Stage 0 owner call: {symbol}"
                    )
        elif observed_calls != expected_calls:
            raise InventoryError(f"critical production AST observation drifted: {symbol}")
    observed_digests = production_surface_digests()
    expected_digests = architecture.get("production_surface_digests")
    if allow_stage2_schema_addition:
        required_stage2 = (
            ROOT
            / "src/pulsara_agent/storage/migrations/sql/0013_conversation_kernel_hard_cut.sql",
            ROOT / "src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto",
        )
        if not all(path.is_file() for path in required_stage2):
            raise InventoryError("Stage 2 schema/protocol hard-cut artifacts are absent")
        if (
            observed_digests["schema_and_protocol"]
            == expected_digests["schema_and_protocol"]
        ):
            raise InventoryError("Stage 2 schema/protocol surface did not advance")
    elif (
        observed_digests["schema_and_protocol"]
        != expected_digests["schema_and_protocol"]
    ):
        raise InventoryError("Stage 1 changed the frozen schema/protocol surface")

    for prefix in DERIVED_UI_PREFIXES:
        root = ROOT / prefix
        paths = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in paths:
            if not path.exists():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                observed_name = (
                    _symbol_name(node.func)
                    if isinstance(node, ast.Call)
                    else _symbol_name(node)
                )
                if observed_name in FORBIDDEN_DERIVED_UI_SYMBOLS:
                    raise InventoryError(
                        f"derived/UI owner gained forbidden authority: {path.relative_to(ROOT)}"
                    )

    for path in sorted((ROOT / "src" / "pulsara_agent").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "durability_subtraction_stage0" in text or (
            "durability_subtraction_inventory" in text
        ):
            raise InventoryError(
                f"production imported Stage 0 test authority: {path.relative_to(ROOT)}"
            )

    report = {
        "event_type_count": len(rows),
        "target_class_counts": dict(sorted(counts.items())),
        "target_counts": EXPECTED_TARGET_COUNTS,
        "owner_count": len(owner_rows),
        "fault_scenario_count": len(scenarios),
        "static_metrics": observed_static_metrics,
        "empirical_metrics": empirical_metrics,
        "production_surface_digests": observed_digests,
    }
    report_bytes = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    report["report_sha256"] = hashlib.sha256(report_bytes).hexdigest()
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = validate()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "Stage 0 inventory valid: "
            f"{report['event_type_count']} EventTypes, "
            f"{report['owner_count']} owners, "
            f"{report['fault_scenario_count']} fault scenarios"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
