"""The lightweight visualization subscription builtin's path adapter."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.conversation_kernel.visualization import (
    VisualizationSource,
    VisualizationSourceKind,
)
from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult
from pulsara_agent.tools.builtins.workspace import WorkspaceTool


@dataclass(slots=True)
class VisualizationRenderTool(WorkspaceTool):
    name: str = "visualization_render"

    def freeze_source(self, source: VisualizationSource) -> VisualizationSource:
        if source.kind is VisualizationSourceKind.PATH:
            return VisualizationSource(
                source.kind, str(self._resolve_read_path(source.value))
            )
        return source

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        raise RuntimeError("visualization_render requires its Host subscription owner")
