"""Small explicit tool registry inspired by Hermes, without plugin sprawl."""

from __future__ import annotations

from dataclasses import dataclass, field

from pulsara_agent.tools.base import AsyncTool, Tool
from pulsara_agent.llm.input import ToolSpec


@dataclass(slots=True)
class ToolRegistry:
    _tools: dict[str, Tool | AsyncTool] = field(default_factory=dict)

    def register(self, tool: Tool | AsyncTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | AsyncTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {name}") from exc

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool | AsyncTool]:
        return [self._tools[name] for name in self.names()]

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
            )
            for tool in self.all()
        )
