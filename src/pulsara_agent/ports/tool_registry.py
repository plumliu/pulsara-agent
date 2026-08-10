"""Process-local execution binding contracts and registry read boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Literal, Protocol, TypeAlias

from pulsara_agent.ports.tool_execution import AsyncTool, Tool
from pulsara_agent.primitives.model_call import sha256_fingerprint


class ToolBindingOrigin(StrEnum):
    BUILTIN = "builtin"
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
class CustomToolBindingContract(_BindingView):
    binding_kind: Literal["custom"]
    base: ToolBindingContractBase
    contract_fact_fingerprint: str


ToolBindingContract: TypeAlias = (
    BuiltinToolBindingContract | CustomToolBindingContract
)


def build_tool_binding_contract(
    *,
    tool_name: str,
    origin: ToolBindingOrigin | str,
    contract_id: str,
    contract_version: str,
    binding_attributes: object | None = None,
) -> ToolBindingContract:
    resolved_origin = ToolBindingOrigin(origin)
    if not tool_name or not contract_id or not contract_version:
        raise ValueError("tool binding name/id/version are required")
    semantic_binding_attributes = binding_attributes
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
                semantic_binding_attributes,
            ],
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


class ToolRegistryReadPort(Protocol):
    def names(self) -> tuple[str, ...]: ...
    def get(self, name: str) -> Tool | AsyncTool: ...
    def binding_contract(self, name: str) -> ToolBindingContract | None: ...


__all__ = [
    "BuiltinToolBindingContract",
    "CustomToolBindingContract",
    "ToolBindingContract",
    "ToolBindingContractBase",
    "ToolBindingOrigin",
    "ToolRegistryReadPort",
    "build_tool_binding_contract",
]
