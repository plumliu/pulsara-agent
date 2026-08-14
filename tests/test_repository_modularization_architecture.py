"""Mechanical-equivalence gates for ConversationKernelRepository."""

from __future__ import annotations

import importlib.util
import ast
import hashlib
import inspect
import json
from pathlib import Path
import pickle
import typing

from pulsara_agent.conversation_kernel.job_catalog import JOB_HANDLER_CATALOG
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools/repository_modularization_inventory.py"
BASELINE = ROOT / "tests/fixtures/repository_modularization_baseline.json"
_INTERNAL_REPOSITORY_PACKAGE = "pulsara_agent.conversation_kernel._repository"
_ROUND7_ADDED_TOP_LEVEL_FUNCTIONS = {"_plan_question_response"}
_ROUND7_CHANGED_TOP_LEVEL_FUNCTIONS = {
    "_prepared_tool_result_manifest",
    "build_prepared_tool_result_acceptance",
}
_ROUND7_ADDED_METHODS = {
    "_subagent_cancellation_drafts",
    "confirm_cancelled_subagent_turn_and_task",
    "read_turn_terminal_outcome",
    "settle_cancelled_subagent_turn_and_task",
}
_ROUND7_CHANGED_METHODS = {
    "_accept_rejected_plan_tool_batch_in_transaction",
    "_confirm_plan_resolution_in_transaction",
    "_confirm_plan_tool_batch_in_transaction",
    "accept_subagent_child",
    "accept_plan_tool_batch",
    "accept_tool_capability_decision",
    "accept_tool_interaction_decision",
    "accept_tool_result",
    "confirm_plan_question_winner",
    "confirm_tool_result_winner",
    "read_turn_status",
    "resolve_plan_question",
    "set_subagent_task_status",
}
_ROUND7_RUNTIME_CHANGED_METHODS = {
    "_confirm_plan_resolution_in_transaction",
    "accept_subagent_child",
    "set_subagent_task_status",
}
# Exact digest of the deliberately changed Round 7 repository slice.  The M0
# fixture remains immutable; all definitions and physical DB calls outside
# this closed allowlist must still equal that historical checkpoint exactly.
_ROUND7_REPOSITORY_DELTA_SHA256 = (
    "f11be8ab473f29aba733e1f094af393c8d745541da502675ac9f16e1579b76a9"
)


def _package_for_source(path: Path) -> str:
    relative = path.relative_to(ROOT / "src").with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    else:
        parts = parts[:-1]
    return ".".join(parts)


def _absolute_import_targets(
    tree: ast.AST,
    *,
    current_package: str,
) -> set[str]:
    """Resolve imported modules and imported aliases to absolute targets."""

    targets: set[str] = set()
    package_parts = current_package.split(".") if current_package else []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            retained = len(package_parts) - (node.level - 1)
            base_parts = package_parts[: max(0, retained)]
            if node.module:
                base_parts.extend(node.module.split("."))
            base = ".".join(base_parts)
        else:
            base = node.module or ""
        if base:
            targets.add(base)
        for alias in node.names:
            if alias.name == "*":
                continue
            targets.add(f"{base}.{alias.name}" if base else alias.name)
    return targets


def _is_internal_repository_target(target: str) -> bool:
    return target == _INTERNAL_REPOSITORY_PACKAGE or target.startswith(
        f"{_INTERNAL_REPOSITORY_PACKAGE}."
    )


def _inventory_module():
    spec = importlib.util.spec_from_file_location(
        "repository_modularization_inventory", TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _baseline() -> dict[str, object]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _without_source_modules(value: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {key: item for key, item in record.items() if key != "source_module"}
        for record in value
    ]


def _closed_owner_calls(
    calls: list[str], renames: dict[str, str]
) -> list[str]:
    return sorted(renames.get(call, call) for call in calls)


def _round7_repository_delta(current: dict[str, object]) -> dict[str, object]:
    changed_functions = (
        _ROUND7_ADDED_TOP_LEVEL_FUNCTIONS | _ROUND7_CHANGED_TOP_LEVEL_FUNCTIONS
    )
    changed_methods = _ROUND7_ADDED_METHODS | _ROUND7_CHANGED_METHODS
    runtime = current["runtime"]
    assert isinstance(runtime, dict)
    dataclasses = runtime["dataclasses"]
    methods = runtime["methods"]
    assert isinstance(dataclasses, dict) and isinstance(methods, dict)
    return {
        "top_level_functions": {
            name: current["top_level_functions"][name]
            for name in sorted(changed_functions)
        },
        "methods": {
            name: current["methods"][name] for name in sorted(changed_methods)
        },
        "runtime_dataclasses": {
            "PreparedToolResultAcceptance": dataclasses[
                "PreparedToolResultAcceptance"
            ]
        },
        "runtime_methods": {
            name: methods[name]
            for name in sorted(
                _ROUND7_ADDED_METHODS | _ROUND7_RUNTIME_CHANGED_METHODS
            )
        },
        "database_calls": _without_source_modules(
            [
                record
                for record in current["database_calls"]
                if record["owner"] in changed_methods
            ]
        ),
        "physical_checkouts": _without_source_modules(
            [
                record
                for record in current["physical_checkouts"]
                if record["owner"] in changed_methods
            ]
        ),
    }


def test_repository_modularization_baseline_is_exact_at_checkpoint() -> None:
    baseline = _baseline()
    assert baseline["checkpoint_head"] == "edbe7aea5518085028657aedc161d8fcbe88bb6b"
    assert baseline["repository_sha256"] == (
        "43669989c424012e84874d15d85ca3d6842f216d025fa2ae2be293166b2b915e"
    )
    assert len(baseline["methods"]) == 128
    assert len(baseline["top_level_functions"]) == 29
    assert len(baseline["all"]) == 34
    assert len(baseline["observed_imports"]) == 41
    assert len(baseline["runtime"]["owned_observed_symbols"]) == 39
    assert len(
        set(baseline["pytest_node_ids"]) - set(baseline["m0_gate_node_ids"])
    ) == 541
    checkouts = baseline["physical_checkouts"]
    assert len(checkouts) == 39
    assert sum(item["classification"] == "direct_operation" for item in checkouts) == 34
    assert sum(
        item["classification"] == "writer_bootstrap_or_renew"
        for item in checkouts
    ) == 2
    assert sum(item["owner"] in {"_writer_transaction", "_job_transaction", "_event_transaction"} for item in checkouts) == 3


def test_repository_modularization_current_contract_matches_baseline() -> None:
    module = _inventory_module()
    current = module.build_inventory(include_pytest_nodes=False)
    baseline = _baseline()
    for key in (
        "all",
        "observed_imports",
        "top_level_classes",
        "closed_owner_renames",
        "override_seams",
    ):
        assert current[key] == baseline[key], key
    for key, added, changed in (
        (
            "top_level_functions",
            _ROUND7_ADDED_TOP_LEVEL_FUNCTIONS,
            _ROUND7_CHANGED_TOP_LEVEL_FUNCTIONS,
        ),
        ("methods", _ROUND7_ADDED_METHODS, _ROUND7_CHANGED_METHODS),
    ):
        assert set(current[key]) == set(baseline[key]) | added
        for name in set(baseline[key]) - changed:
            assert current[key][name] == baseline[key][name], (key, name)
    current_runtime = current["runtime"]
    baseline_runtime = baseline["runtime"]
    for key in set(baseline_runtime) - {"dataclasses", "methods"}:
        assert current_runtime[key] == baseline_runtime[key], ("runtime", key)
    assert set(current_runtime["dataclasses"]) == set(
        baseline_runtime["dataclasses"]
    )
    for name in set(baseline_runtime["dataclasses"]) - {
        "PreparedToolResultAcceptance"
    }:
        assert current_runtime["dataclasses"][name] == baseline_runtime[
            "dataclasses"
        ][name]
    assert set(current_runtime["methods"]) == set(baseline_runtime["methods"]) | (
        _ROUND7_ADDED_METHODS
    )
    for name in set(baseline_runtime["methods"]) - _ROUND7_RUNTIME_CHANGED_METHODS:
        assert current_runtime["methods"][name] == baseline_runtime["methods"][
            name
        ]
    assert _closed_owner_calls(
        current["class_qualified_calls"], baseline["closed_owner_renames"]
    ) == _closed_owner_calls(
        baseline["class_qualified_calls"], baseline["closed_owner_renames"]
    )
    changed_owners = _ROUND7_ADDED_METHODS | _ROUND7_CHANGED_METHODS
    for key in ("database_calls", "physical_checkouts"):
        current_unchanged = _without_source_modules(
            [item for item in current[key] if item["owner"] not in changed_owners]
        )
        baseline_unchanged = _without_source_modules(
            [item for item in baseline[key] if item["owner"] not in changed_owners]
        )
        assert current_unchanged == baseline_unchanged, key
    encoded_delta = json.dumps(
        _round7_repository_delta(current),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert hashlib.sha256(encoded_delta).hexdigest() == (
        _ROUND7_REPOSITORY_DELTA_SHA256
    )
    import pulsara_agent.conversation_kernel.repository as repository

    for name in baseline["runtime"]["owned_observed_symbols"]:
        value = getattr(repository, name)
        assert pickle.loads(pickle.dumps(value)) is value
    for name in repository.__all__:
        value = getattr(repository, name)
        if inspect.isclass(value) or inspect.isfunction(value):
            typing.get_type_hints(value)


def test_repository_modularization_preserves_every_existing_pytest_node() -> None:
    module = _inventory_module()
    baseline_nodes = set(_baseline()["pytest_node_ids"])
    current_nodes = set(module._pytest_node_ids())
    assert baseline_nodes <= current_nodes


def test_repository_modularization_facade_and_internal_owner_shape() -> None:
    facade = ROOT / "src/pulsara_agent/conversation_kernel/repository.py"
    implementation = ROOT / "src/pulsara_agent/conversation_kernel/_repository"
    assert facade.exists()
    if implementation.exists():
        forbidden = {"_monolith.py", "monolith.py", "legacy.py"}
        assert not {path.name for path in implementation.glob("*.py")} & forbidden
        for path in implementation.glob("*.py"):
            assert "pulsara_agent.conversation_kernel.repository" not in path.read_text(
                encoding="utf-8"
            )
        facade_source = facade.read_text(encoding="utf-8")
        assert len(facade_source.splitlines()) < 256
        assert "pulsara_v3." not in facade_source
        assert "._provider.connection" not in facade_source
        provider_owners = [
            path.name
            for path in implementation.glob("*.py")
            if "VerifiedPostgresConnectionProviderProtocol"
            in path.read_text(encoding="utf-8")
        ]
        assert provider_owners == ["kernel.py"]
        for pure_name in ("contracts.py", "matching.py"):
            pure = (implementation / pure_name).read_text(encoding="utf-8")
            assert "psycopg" not in pure
            assert "postgres_connection_provider" not in pure
        assert "def _load_root_transcript_cut(" in (
            implementation / "jobs.py"
        ).read_text(encoding="utf-8")
        assert "def _content_from_row(" in (
            implementation / "matching.py"
        ).read_text(encoding="utf-8")

    assert ConversationKernelRepository.__module__ == (
        "pulsara_agent.conversation_kernel.repository"
    )
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 34
    assert len(LIVE_EVENT_TYPES) == 23
    assert len(SUBJECT_SLOTS) == 15
    assert len(APPEND_GUARDS) == 2
    assert len(CONVERSATION_KERNEL_RELATIONS) == 26
    assert len(JOB_HANDLER_CATALOG) == 4


def test_repository_modularization_internal_package_is_not_a_second_public_api() -> None:
    implementation = ROOT / "src/pulsara_agent/conversation_kernel/_repository"
    production = ROOT / "src/pulsara_agent"
    relative_targets = _absolute_import_targets(
        ast.parse("from ._repository.contracts import FrozenContent"),
        current_package="pulsara_agent.conversation_kernel",
    )
    assert "pulsara_agent.conversation_kernel._repository.contracts" in (
        relative_targets
    )
    alias_targets = _absolute_import_targets(
        ast.parse("from pulsara_agent.conversation_kernel import _repository"),
        current_package="pulsara_agent.conversation_kernel",
    )
    assert _INTERNAL_REPOSITORY_PACKAGE in alias_targets
    for path in production.rglob("*.py"):
        if path == ROOT / "src/pulsara_agent/conversation_kernel/repository.py":
            continue
        if implementation in path.parents:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = _absolute_import_targets(
            tree,
            current_package=_package_for_source(path),
        )
        assert not any(_is_internal_repository_target(item) for item in imports), path
