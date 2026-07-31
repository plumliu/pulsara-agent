"""Small explicit tool registry inspired by Hermes, without plugin sprawl."""

from __future__ import annotations

from dataclasses import dataclass, field

from pulsara_agent.ports.tool_execution import AsyncTool, Tool
from pulsara_agent.ports.tool_registry import (
    McpToolBindingContract,
    ToolBindingContract,
    build_tool_binding_contract,
)


@dataclass(slots=True)
class ToolRegistry:
    _tools: dict[str, Tool | AsyncTool] = field(default_factory=dict)
    _binding_contracts: dict[str, ToolBindingContract] = field(default_factory=dict)

    def register(
        self,
        tool: Tool | AsyncTool,
        *,
        binding_contract: ToolBindingContract | None = None,
    ) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        if binding_contract is not None and binding_contract.tool_name != tool.name:
            raise ValueError("tool binding contract name mismatch")
        self._tools[tool.name] = tool
        if binding_contract is not None:
            self._binding_contracts[tool.name] = binding_contract

    def get(self, name: str) -> Tool | AsyncTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def all(self) -> tuple[Tool | AsyncTool, ...]:
        return tuple(self._tools[name] for name in self.names())

    def binding_contract(self, name: str) -> ToolBindingContract | None:
        return self._binding_contracts.get(name)

    def bind_contract(self, contract: ToolBindingContract) -> None:
        """Attach a contract to an already registered execution binding."""

        if contract.tool_name not in self._tools:
            raise KeyError(f"Unknown tool: {contract.tool_name}")
        existing = self._binding_contracts.get(contract.tool_name)
        if existing is not None and existing != contract:
            raise ValueError(
                f"Tool binding contract already frozen: {contract.tool_name}"
            )
        self._binding_contracts[contract.tool_name] = contract

    def binding_contracts(self) -> tuple[ToolBindingContract, ...]:
        return tuple(
            self._binding_contracts[name] for name in sorted(self._binding_contracts)
        )

    def mcp_bindings(self) -> tuple[McpToolBindingContract, ...]:
        return tuple(
            contract
            for contract in self.binding_contracts()
            if isinstance(contract, McpToolBindingContract)
        )

    def restricted_to(self, allowed_names: frozenset[str]) -> ToolRegistry:
        """Return a registry containing only one frozen execution surface subset."""

        unknown = allowed_names.difference(self._tools)
        if unknown:
            raise ValueError(
                "Cannot restrict ToolRegistry to unknown tools: "
                + ", ".join(sorted(unknown))
            )
        restricted = ToolRegistry()
        for name in sorted(allowed_names):
            restricted.register(
                self._tools[name],
                binding_contract=self._binding_contracts.get(name),
            )
        return restricted


__all__ = ["ToolRegistry", "build_tool_binding_contract"]
