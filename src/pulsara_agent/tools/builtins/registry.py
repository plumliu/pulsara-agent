"""Registry construction for Pulsara built-in tools."""

from __future__ import annotations

from pulsara_agent.runtime.session import RuntimeSession

from pulsara_agent.tools.builtins.filesystem import (
    EditFileTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from pulsara_agent.tools.builtins.terminal import TerminalTool
from pulsara_agent.tools.builtins.todo import TodoTool
from pulsara_agent.tools.registry import ToolRegistry


def build_core_tool_registry(runtime_session: RuntimeSession) -> ToolRegistry:
    if not isinstance(runtime_session, RuntimeSession):
        raise TypeError("build_core_tool_registry requires a RuntimeSession")
    root = runtime_session.workspace_root
    registry = ToolRegistry()
    registry.register(ReadFileTool(root))
    registry.register(SearchFilesTool(root))
    registry.register(TerminalTool(root, runtime_session.terminal_sessions))
    registry.register(EditFileTool(root))
    registry.register(WriteFileTool(root))
    registry.register(TodoTool())
    return registry
