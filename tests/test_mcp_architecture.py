from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "pulsara_agent"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_mcp_low_level_contracts_do_not_import_host_or_agent_runtime() -> None:
    for relative in (
        "primitives/mcp.py",
        "runtime/mcp/types.py",
    ):
        modules = _imported_modules(SRC / relative)
        assert not any(module.startswith("pulsara_agent.host") for module in modules)
        assert "pulsara_agent.runtime.agent" not in modules


def test_mcp_sdk_and_worker_do_not_import_capability_or_host_mutation_layers() -> None:
    sdk_modules = _imported_modules(SRC / "runtime/mcp/sdk.py")
    worker_modules = _imported_modules(SRC / "runtime/mcp/supervisor.py")
    assert not any(
        module.startswith("pulsara_agent.capability") for module in sdk_modules
    )
    assert not any(module.startswith("pulsara_agent.host") for module in worker_modules)


def test_mcp_removed_production_symbols_do_not_reappear() -> None:
    source_text = "\n".join(
        path.read_text()
        for path in SRC.rglob("*.py")
    )
    removed_symbols = (
        "McpCapabilityBindingBundle",
        "build_mcp_bundle",
        "runtime_wiring.mcp_manager",
        "runtime_wiring.mcp_bundle",
        "SdkMcpClientManager.start",
        "startup_timeout_ms",
    )
    for symbol in removed_symbols:
        assert symbol not in source_text
