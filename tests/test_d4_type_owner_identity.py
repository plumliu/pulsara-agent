from __future__ import annotations

import ast
from pathlib import Path
from types import UnionType
from typing import Annotated, Literal, get_args, get_origin

from pulsara_agent.ports.mcp import McpToolExecutionOutcome
from pulsara_agent.ports.terminal import TerminalProcessInput
from pulsara_agent.ports.tool_execution import Tool, ToolCall
from pulsara_agent.ports.tool_registry import ToolBindingContract
from pulsara_agent.primitives.subagent import SubagentStatus


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "pulsara_agent"


def _assignment_count(path: Path, symbol: str) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            count += sum(
                isinstance(target, ast.Name) and target.id == symbol
                for target in node.targets
            )
        elif isinstance(node, ast.AnnAssign):
            count += int(isinstance(node.target, ast.Name) and node.target.id == symbol)
    return count


def test_class_and_protocol_symbols_have_one_final_owner() -> None:
    assert ToolCall.__module__ == "pulsara_agent.ports.tool_execution"
    assert Tool.__module__ == "pulsara_agent.ports.tool_execution"
    assert Tool.__qualname__ == "Tool"
    assert not (SRC / "tools" / "base.py").exists()


def test_assignment_alias_uses_shape_not_typing_runtime_metadata() -> None:
    assert get_origin(SubagentStatus) is Literal
    assert get_args(SubagentStatus) == (
        "running",
        "suspended",
        "completed",
        "failed",
        "cancelled",
    )
    assert getattr(SubagentStatus, "__module__") == "typing"
    assert _assignment_count(SRC / "primitives" / "subagent.py", "SubagentStatus") == 1
    assert not (SRC / "runtime" / "subagent" / "types.py").exists()


def test_union_aliases_preserve_ordered_final_owner_shape() -> None:
    assert get_origin(ToolBindingContract) is UnionType
    assert tuple(item.__module__ for item in get_args(ToolBindingContract)) == (
        "pulsara_agent.ports.tool_registry",
        "pulsara_agent.ports.tool_registry",
        "pulsara_agent.ports.tool_registry",
    )
    assert get_origin(McpToolExecutionOutcome) is UnionType
    assert tuple(item.__name__ for item in get_args(McpToolExecutionOutcome)) == (
        "McpToolCompletedOutcome",
        "McpToolSuspendedOutcome",
        "McpToolRejectedOutcome",
    )


def test_annotated_discriminated_alias_has_one_final_ast_owner() -> None:
    assert get_origin(TerminalProcessInput) is Annotated
    process_union = get_args(TerminalProcessInput)[0]
    assert get_origin(process_union) is UnionType
    assert len(get_args(process_union)) == 8
    assert _assignment_count(SRC / "ports" / "terminal.py", "TerminalProcessInput") == 1
    assert not (SRC / "terminal_public_api.py").exists()
