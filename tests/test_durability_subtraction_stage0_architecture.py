"""Machine-checkable Stage 0 durability subtraction evidence."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "durability_subtraction_inventory.py"


def _inventory_module():
    spec = importlib.util.spec_from_file_location(
        "durability_subtraction_inventory", TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json_fixture(name: str):
    return json.loads((ROOT / "tests" / "fixtures" / name).read_text(encoding="utf-8"))


def test_stage0_inventory_manifest_target_owner_and_fault_matrix_are_exact() -> None:
    report = _inventory_module().validate(allow_stage2_schema_addition=True)
    baseline = _json_fixture("durability_subtraction_stage0_baseline.json")

    assert report["event_type_count"] == 151
    assert report["target_class_counts"] == {"A": 39, "B": 25, "C": 16, "D": 71}
    assert report["target_counts"] == {
        "committed_core": 26,
        "live_core": 23,
        "subject_slots": 13,
        "append_guards": 2,
    }
    assert report["owner_count"] == 27
    assert report["fault_scenario_count"] == 12
    assert report["static_metrics"] == {
        "schema_registry_version": 11,
        "sql_create_table_count": 86,
        "foreground_committed_reducers": 9,
        "mainline_reconciliation_latches": 6,
        "host_close_await_expressions": 45,
        "host_close_committed_reducer_barriers": 4,
        "non_host_runtime_teardown_await_expressions": 11,
    }
    assert report["empirical_metrics"] == {
        "text_only_universal_events": 43,
        "one_tool_universal_events": 83,
        "text_steady_state_durable_write_scope_minimum": 15,
        "one_tool_steady_state_durable_write_scope_minimum": 31,
        "normal_model_call_audit_artifacts": 4,
    }
    assert baseline["stage0_read_only_probes"]["inventory"]["report_sha256"]


def test_stage0_frozen_documents_match_recorded_sha256() -> None:
    baseline = _json_fixture("durability_subtraction_stage0_baseline.json")
    for relative, expected in baseline["document_sha256"].items():
        actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        assert actual == expected, relative


def test_stage0_fault_matrix_references_collected_test_functions() -> None:
    matrix = _json_fixture("durability_subtraction_stage0_fault_matrix.json")
    for scenario in matrix["scenarios"]:
        for test_id in scenario["test_ids"]:
            relative, function_name = test_id.split("::", 1)
            path = ROOT / relative
            assert path.is_file(), test_id
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            functions = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            assert function_name in functions, test_id


def test_stage0_critical_callers_are_semantic_ast_allowlist_not_line_numbers() -> None:
    observations = _inventory_module().architecture_observations()

    assert observations["PreparedContextInputAuditSourceCapture"] == []
    assert observations["bind_optional_context_audit_source"] == []
    assert observations["offer_best_effort_nowait"] == []
    assert (
        sum(
            row["count"]
            for row in observations["latch_publication_reconciliation_required"]
        )
        == 3
    )
    assert (
        sum(
            row["count"]
            for row in observations["RuntimeProjectionCheckpointAdmissionBlocked"]
        )
        == 1
    )
    assert (
        sum(
            row["count"]
            for row in observations["await_committed_reducer_repair_safe_point"]
        )
        == 8
    )
    assert (
        sum(
            row["count"] for row in observations["drain_open_committed_reducer_barrier"]
        )
        == 5
    )


def test_stage0_physical_operation_calls_have_an_ast_owner() -> None:
    observations = _inventory_module().architecture_observations()
    for symbol in (
        "create_task",
        "shield",
        "gather",
        "wait_for",
        "to_thread",
        "run_in_executor",
    ):
        rows = observations[symbol]
        assert rows, symbol
        assert all(row["owner"] != "<module>" for row in rows), symbol
        assert all(row["count"] >= 1 for row in rows), symbol


def test_stage2_activation_has_one_new_conversation_authority() -> None:
    changed = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--", "src/pulsara_agent"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "src/pulsara_agent/cli.py" in changed
    cli = (ROOT / "src/pulsara_agent/cli.py").read_text(encoding="utf-8")
    assert "KernelHostCore.production" in cli
    assert "_kernel_host_run(args)" in cli
    assert "_kernel_host_repl(args)" in cli
    assert "_kernel_host_tui(args)" in cli

    kernel_root = ROOT / "src/pulsara_agent/conversation_kernel"
    assert kernel_root.is_dir()
    product_relations = set(
        _json_fixture("durability_subtraction_stage0_target_oracle.json")[
            "committed_core"
        ][0].keys()
    )
    assert product_relations  # oracle remains readable; production owns no copy.
    for path in sorted(kernel_root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r"(?:FROM|INTO|UPDATE|JOIN|TABLE)\s+([a-z_][a-z0-9_]*)",
            source,
            flags=re.IGNORECASE,
        ):
            assert match.group(1).lower() not in {
                "sessions",
                "turns",
                "transcript_entries",
                "durable_jobs",
                "agent_events",
            }, (path, match.group(0))

    forbidden = {
        "durability_subtraction_stage0",
        "durability_subtraction_inventory",
    }
    for path in sorted((ROOT / "src" / "pulsara_agent").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert not any(item in text for item in forbidden), path


def test_stage0_target_oracle_is_not_a_production_type_owner() -> None:
    target = _json_fixture("durability_subtraction_stage0_target_oracle.json")
    formal_names = {item["type"] for item in target["committed_core"]} | set(
        target["live_core"]
    )
    production_classes: set[str] = set()
    for path in sorted((ROOT / "src" / "pulsara_agent").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        production_classes.update(
            node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        )
    assert formal_names.isdisjoint(production_classes)
    assert not any(name.startswith("RawProvider") for name in formal_names)
    assert "ToolOutcomeUnknown" not in formal_names
    assert "CustomEvent" not in formal_names


def test_stage0_runbook_freezes_reset_only_sequence_without_executing_it() -> None:
    runbook = (ROOT / "DURABILITY_SUBTRACTION_CUTOVER_RUNBOOK.zh.md").read_text(
        encoding="utf-8"
    )
    required = (
        "停止新 admission",
        "fence 旧 writer 与 worker",
        "取消并 join 当前进程 physical owner",
        "隔离仍在外部运行的 process/effect",
        "清空 Pulsara-owned PostgreSQL schema/data",
        "清空 shared blob 与 derived plane",
        "从 empty store 执行 Stage 2 migration",
        "rollback 只能再次 complete reset",
        "不提供旧 authority 导入、转换或反向投影",
    )
    assert all(item in runbook for item in required)
    assert "未执行" in runbook


def test_stage0_documents_have_closed_fences_and_valid_local_links() -> None:
    documents = (
        ROOT / "PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md",
        ROOT / "STAGE_0_1_IMPLEMENTATION_SPEC.zh.md",
        ROOT / "DURABILITY_SUBTRACTION_CUTOVER_RUNBOOK.zh.md",
    )
    link_pattern = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
    for document in documents:
        text = document.read_text(encoding="utf-8")
        assert text.count("```") % 2 == 0, document
        assert text.count("~~~") % 2 == 0, document
        assert all(not line.endswith((" ", "\t")) for line in text.splitlines())
        for raw_target in link_pattern.findall(text):
            target = raw_target.split("#", 1)[0].strip().strip("<>")
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            assert (document.parent / target).exists(), (
                document.relative_to(ROOT),
                raw_target,
            )
