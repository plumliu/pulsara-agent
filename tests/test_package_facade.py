from __future__ import annotations

import ast
import importlib
from pathlib import Path

from pulsara_agent.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "pulsara_agent"


def test_runtime_facade_is_empty_and_eager() -> None:
    runtime = importlib.import_module("pulsara_agent.runtime")
    assert runtime.__all__ == []
    assert "__getattr__" not in vars(runtime)
    for removed in (
        "AgentRuntime",
        "RuntimeSession",
        "ToolCall",
        "ToolExecutor",
        "build_in_memory_runtime_wiring",
    ):
        assert not hasattr(runtime, removed)


def test_tools_facade_only_eagerly_exports_tool_registry() -> None:
    tools = importlib.import_module("pulsara_agent.tools")
    assert tools.__all__ == ["ToolRegistry"]
    assert tools.ToolRegistry is ToolRegistry
    assert "__getattr__" not in vars(tools)
    for removed in (
        "ToolCall",
        "ToolExecutor",
        "build_core_tool_registry",
        "TerminalTool",
    ):
        assert not hasattr(tools, removed)


def test_production_source_uses_owning_modules_instead_of_package_facades() -> None:
    violations: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module == "pulsara_agent.runtime":
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:runtime")
            if node.module == "pulsara_agent.tools":
                imported = tuple(alias.name for alias in node.names)
                if imported != ("ToolRegistry",):
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:tools:{imported}"
                    )
    assert violations == []


def test_removed_cycle_hiding_modules_are_physically_absent() -> None:
    removed = (
        "src/pulsara_agent/tools/base.py",
        "src/pulsara_agent/tools/executor.py",
        "src/pulsara_agent/runtime/tool_action.py",
        "src/pulsara_agent/runtime/tool_taxonomy.py",
        "src/pulsara_agent/runtime/terminal_risk.py",
    )
    assert all(not (ROOT / relative).exists() for relative in removed)
