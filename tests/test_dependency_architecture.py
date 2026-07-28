from __future__ import annotations

import ast
from pathlib import Path

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog
from pulsara_agent.capability.builtin_provider import builtin_tool_descriptors
from tests.support.dependency_rules import (
    forbidden_d4_observations,
    observation_set_fingerprint,
    residual_scc_observations,
    scan_pulsara_imports,
)


ROOT = Path(__file__).resolve().parents[1]

# Updated only when a hard cut deliberately removes residual edges. D4 forbids growth.
RESIDUAL_SCC_OBSERVATION_FINGERPRINT = (
    "sha256:3714e6d2b587364c3636a249feb2fc6d2171edfc2f5c802278e957562e7126cc"
)


def test_d4_target_dependency_dag_has_no_exceptions() -> None:
    observations = scan_pulsara_imports(ROOT)
    assert forbidden_d4_observations(observations) == ()


def test_package_scc_diagnostic_baseline_does_not_grow() -> None:
    residual = residual_scc_observations(scan_pulsara_imports(ROOT))
    assert observation_set_fingerprint(residual) == RESIDUAL_SCC_OBSERVATION_FINGERPRINT


def test_canonical_ast_import_observation_detects_same_scc_package_pair_growth(
    tmp_path: Path,
) -> None:
    package = tmp_path / "src" / "pulsara_agent"
    (package / "runtime").mkdir(parents=True)
    (package / "memory").mkdir(parents=True)
    (package / "runtime" / "a.py").write_text(
        "from pulsara_agent.memory import first\n", encoding="utf-8"
    )
    (package / "memory" / "a.py").write_text(
        "from pulsara_agent.runtime import first\n", encoding="utf-8"
    )
    baseline = residual_scc_observations(scan_pulsara_imports(tmp_path))
    (package / "runtime" / "b.py").write_text(
        "def load():\n    from pulsara_agent.memory import second\n",
        encoding="utf-8",
    )
    changed = residual_scc_observations(scan_pulsara_imports(tmp_path))
    assert observation_set_fingerprint(changed) != observation_set_fingerprint(baseline)


def test_legal_acyclic_import_does_not_count_as_scc_growth(tmp_path: Path) -> None:
    package = tmp_path / "src" / "pulsara_agent"
    (package / "runtime").mkdir(parents=True)
    (package / "ports").mkdir(parents=True)
    (package / "runtime" / "a.py").write_text(
        "from pulsara_agent.ports import tool_execution\n", encoding="utf-8"
    )
    assert residual_scc_observations(scan_pulsara_imports(tmp_path)) == ()


def test_import_observation_identity_ignores_source_attribution_lines(
    tmp_path: Path,
) -> None:
    package = tmp_path / "src" / "pulsara_agent" / "runtime"
    package.mkdir(parents=True)
    module = package / "sample.py"
    module.write_text(
        "from pulsara_agent.memory import first\n",
        encoding="utf-8",
    )
    before = scan_pulsara_imports(tmp_path)
    module.write_text(
        "# diagnostic attribution moved\n\nfrom pulsara_agent.memory import first\n",
        encoding="utf-8",
    )
    after = scan_pulsara_imports(tmp_path)
    assert tuple(item.observation_id for item in before) == tuple(
        item.observation_id for item in after
    )
    assert tuple(item.observation_fingerprint for item in before) == tuple(
        item.observation_fingerprint for item in after
    )
    assert before[0].line != after[0].line


def test_import_scanner_covers_closed_import_kinds(tmp_path: Path) -> None:
    package = tmp_path / "src" / "pulsara_agent" / "runtime"
    package.mkdir(parents=True)
    (package / "sample.py").write_text(
        """
import importlib
import pulsara_agent.memory
from pulsara_agent.host import core

importlib.import_module("pulsara_agent.graph")
__import__("pulsara_agent.storage")

def __getattr__(name):
    return importlib.import_module("pulsara_agent.capability")
""".lstrip(),
        encoding="utf-8",
    )
    observations = scan_pulsara_imports(tmp_path)
    assert {item.import_kind for item in observations} == {
        "import",
        "from",
        "import_module",
        "dunder_import",
        "package_getattr",
    }


def test_builtin_tool_catalog_is_exhaustive() -> None:
    catalog = builtin_tool_catalog()
    descriptors = builtin_tool_descriptors()
    assert tuple(item.name for item in catalog) == tuple(
        item.name for item in descriptors
    )
    assert len({item.entry_fingerprint for item in catalog}) == len(catalog)


def test_d4_has_no_temporary_type_reexports() -> None:
    removed = (
        "src/pulsara_agent/tools/base.py",
        "src/pulsara_agent/tools/executor.py",
        "src/pulsara_agent/runtime/subagent/types.py",
        "src/pulsara_agent/runtime/tool_taxonomy.py",
        "src/pulsara_agent/runtime/terminal_risk.py",
        "src/pulsara_agent/runtime/projection_jobs/contracts.py",
        "src/pulsara_agent/runtime/projection_jobs/migration_state.py",
    )
    assert all(not (ROOT / item).exists() for item in removed)


def test_production_composition_has_no_in_memory_or_mock_binding() -> None:
    source = (ROOT / "src/pulsara_agent/host/production_composition.py").read_text(
        encoding="utf-8"
    )
    assert "MockMcpClientManager" not in source
    assert "build_in_memory_runtime_wiring" not in source
    assert "InMemory" not in source


def test_host_core_has_no_durable_selector() -> None:
    tree = ast.parse(
        (ROOT / "src/pulsara_agent/host/core.py").read_text(encoding="utf-8")
    )
    host = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "HostCore"
    )
    assert not any(
        isinstance(node, (ast.AnnAssign, ast.Assign))
        and (
            (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "durable"
            )
            or (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "durable"
                    for target in node.targets
                )
            )
        )
        for node in host.body
    )


def test_runtime_and_tools_facades_have_no_lazy_router() -> None:
    for relative in (
        "src/pulsara_agent/runtime/__init__.py",
        "src/pulsara_agent/tools/__init__.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "_LAZY_EXPORTS" not in source
        assert "def __getattr__" not in source
