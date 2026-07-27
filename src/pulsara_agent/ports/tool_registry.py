"""Process-local execution binding contracts and registry read boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Literal, Protocol, TypeAlias

from pulsara_agent.ports.tool_execution import AsyncTool, Tool
from pulsara_agent.primitives.mcp import McpBindingIdentityFact
from pulsara_agent.primitives.model_call import sha256_fingerprint


class ToolBindingOrigin(StrEnum):
    BUILTIN = "builtin"
    MCP = "mcp"
    CUSTOM = "custom"
    WORKFLOW = "workflow"
    SUBAGENT_SYSTEM = "subagent_system"


@dataclass(frozen=True, slots=True)
class ToolBindingContractBase:
    tool_name: str
    origin: ToolBindingOrigin
    contract_id: str
    contract_version: str
    binding_fingerprint: str


class _BindingView:
    base: ToolBindingContractBase

    @property
    def tool_name(self) -> str:
        return self.base.tool_name

    @property
    def origin(self) -> ToolBindingOrigin:
        return self.base.origin

    @property
    def contract_id(self) -> str:
        return self.base.contract_id

    @property
    def contract_version(self) -> str:
        return self.base.contract_version

    @property
    def binding_fingerprint(self) -> str:
        return self.base.binding_fingerprint


@dataclass(frozen=True, slots=True)
class BuiltinToolBindingContract(_BindingView):
    binding_kind: Literal["builtin"]
    base: ToolBindingContractBase
    contract_fact_fingerprint: str


@dataclass(frozen=True, slots=True)
class McpToolBindingContract(_BindingView):
    binding_kind: Literal["mcp"]
    base: ToolBindingContractBase
    binding_identity: McpBindingIdentityFact
    original_tool_name: str
    contract_fact_fingerprint: str


@dataclass(frozen=True, slots=True)
class CustomToolBindingContract(_BindingView):
    binding_kind: Literal["custom"]
    base: ToolBindingContractBase
    contract_fact_fingerprint: str


ToolBindingContract: TypeAlias = (
    BuiltinToolBindingContract | McpToolBindingContract | CustomToolBindingContract
)


def build_tool_binding_contract(
    *,
    tool_name: str,
    origin: ToolBindingOrigin | str,
    contract_id: str,
    contract_version: str,
    binding_attributes: object | None = None,
    mcp_binding_identity: McpBindingIdentityFact | None = None,
    original_tool_name: str | None = None,
) -> ToolBindingContract:
    resolved_origin = ToolBindingOrigin(origin)
    if not tool_name or not contract_id or not contract_version:
        raise ValueError("tool binding name/id/version are required")
    base = ToolBindingContractBase(
        tool_name=tool_name,
        origin=resolved_origin,
        contract_id=contract_id,
        contract_version=contract_version,
        binding_fingerprint=sha256_fingerprint(
            "tool-binding-contract:v1",
            [
                tool_name,
                resolved_origin.value,
                contract_id,
                contract_version,
                binding_attributes,
            ],
        ),
    )
    if resolved_origin is ToolBindingOrigin.MCP:
        identity = mcp_binding_identity or _mcp_identity_from_attributes(
            binding_attributes
        )
        if identity is None or not original_tool_name:
            raise ValueError("MCP binding requires exact identity and original name")
        payload = {
            "binding_kind": "mcp",
            "base": asdict(base),
            "binding_identity": identity.model_dump(mode="json"),
            "original_tool_name": original_tool_name,
        }
        return McpToolBindingContract(
            binding_kind="mcp",
            base=base,
            binding_identity=identity,
            original_tool_name=original_tool_name,
            contract_fact_fingerprint=sha256_fingerprint(
                "tool-binding-contract-fact:v1", payload
            ),
        )
    if resolved_origin is ToolBindingOrigin.CUSTOM:
        payload = {"binding_kind": "custom", "base": asdict(base)}
        return CustomToolBindingContract(
            binding_kind="custom",
            base=base,
            contract_fact_fingerprint=sha256_fingerprint(
                "tool-binding-contract-fact:v1", payload
            ),
        )
    payload = {"binding_kind": "builtin", "base": asdict(base)}
    return BuiltinToolBindingContract(
        binding_kind="builtin",
        base=base,
        contract_fact_fingerprint=sha256_fingerprint(
            "tool-binding-contract-fact:v1", payload
        ),
    )


def _mcp_identity_from_attributes(
    value: object | None,
) -> McpBindingIdentityFact | None:
    if not isinstance(value, dict):
        return None
    required = ("server_id", "slot_id", "snapshot_id", "discovery_generation")
    if any(value.get(key) is None for key in required):
        return None
    return McpBindingIdentityFact(
        server_id=str(value["server_id"]),
        slot_id=str(value["slot_id"]),
        snapshot_id=str(value["snapshot_id"]),
        discovery_generation=int(value["discovery_generation"]),
    )


class ToolRegistryReadPort(Protocol):
    def names(self) -> tuple[str, ...]: ...
    def get(self, name: str) -> Tool | AsyncTool: ...
    def binding_contract(self, name: str) -> ToolBindingContract | None: ...
    def mcp_bindings(self) -> tuple[McpToolBindingContract, ...]: ...


__all__ = [
    "BuiltinToolBindingContract",
    "CustomToolBindingContract",
    "McpToolBindingContract",
    "ToolBindingContract",
    "ToolBindingContractBase",
    "ToolBindingOrigin",
    "ToolRegistryReadPort",
    "build_tool_binding_contract",
]
