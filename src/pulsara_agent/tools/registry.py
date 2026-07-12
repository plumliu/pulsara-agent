"""Small explicit tool registry inspired by Hermes, without plugin sprawl."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pulsara_agent.tools.base import AsyncTool, Tool
from pulsara_agent.llm.input import ToolSpec
from pulsara_agent.primitives.model_call import sha256_fingerprint


@dataclass(frozen=True, slots=True)
class ToolBindingContract:
    tool_name: str
    origin: Literal["builtin", "mcp", "custom", "workflow", "subagent_system"]
    contract_id: str
    contract_version: str
    binding_fingerprint: str


def build_tool_binding_contract(
    *,
    tool_name: str,
    origin: Literal["builtin", "mcp", "custom", "workflow", "subagent_system"],
    contract_id: str,
    contract_version: str,
    binding_attributes: object | None = None,
) -> ToolBindingContract:
    if not contract_id or not contract_version:
        raise ValueError("tool binding contract id/version are required")
    return ToolBindingContract(
        tool_name=tool_name,
        origin=origin,
        contract_id=contract_id,
        contract_version=contract_version,
        binding_fingerprint=sha256_fingerprint(
            "tool-binding-contract:v1",
            [
                tool_name,
                origin,
                contract_id,
                contract_version,
                binding_attributes,
            ],
        ),
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

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool | AsyncTool]:
        return [self._tools[name] for name in self.names()]

    def binding_contract(self, name: str) -> ToolBindingContract | None:
        return self._binding_contracts.get(name)

    def bind_contract(self, contract: ToolBindingContract) -> None:
        """Attach a contract to an already registered execution binding."""

        if contract.tool_name not in self._tools:
            raise KeyError(f"Unknown tool: {contract.tool_name}")
        existing = self._binding_contracts.get(contract.tool_name)
        if existing is not None and existing != contract:
            raise ValueError(f"Tool binding contract already frozen: {contract.tool_name}")
        self._binding_contracts[contract.tool_name] = contract

    def binding_contracts(self) -> tuple[ToolBindingContract, ...]:
        return tuple(
            self._binding_contracts[name] for name in sorted(self._binding_contracts)
        )

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
            )
            for tool in self.all()
        )
