from __future__ import annotations

import pytest

from pulsara_agent.ports.tool_registry import (
    BuiltinToolBindingContract,
    CustomToolBindingContract,
    McpToolBindingContract,
    ToolBindingOrigin,
    build_tool_binding_contract,
)
from pulsara_agent.primitives.mcp import McpBindingIdentityFact
from pulsara_agent.tools.registry import ToolRegistry


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name

    def execute(self, call):
        raise AssertionError(call)


def _identity(generation: int = 1) -> McpBindingIdentityFact:
    return McpBindingIdentityFact(
        server_id="docs",
        slot_id="mcp_slot:docs",
        snapshot_id="mcp_snapshot:docs",
        discovery_generation=generation,
    )


def test_binding_factory_returns_the_closed_origin_union() -> None:
    builtin = build_tool_binding_contract(
        tool_name="read_file",
        origin=ToolBindingOrigin.BUILTIN,
        contract_id="pulsara.builtin.read_file",
        contract_version="v1",
    )
    custom = build_tool_binding_contract(
        tool_name="custom_probe",
        origin=ToolBindingOrigin.CUSTOM,
        contract_id="custom.probe",
        contract_version="v1",
    )
    mcp = build_tool_binding_contract(
        tool_name="mcp__docs__lookup",
        origin=ToolBindingOrigin.MCP,
        contract_id="pulsara.mcp.docs.lookup",
        contract_version="v1",
        binding_attributes=_identity().model_dump(mode="json"),
        mcp_binding_identity=_identity(),
        original_tool_name="lookup",
    )

    assert isinstance(builtin, BuiltinToolBindingContract)
    assert isinstance(custom, CustomToolBindingContract)
    assert isinstance(mcp, McpToolBindingContract)
    assert mcp.binding_identity == _identity()
    assert mcp.original_tool_name == "lookup"


def test_mcp_semantic_binding_includes_original_tool_and_occurrence_identity() -> None:
    common = dict(
        tool_name="mcp__docs__lookup",
        origin=ToolBindingOrigin.MCP,
        contract_id="pulsara.mcp.docs.lookup",
        contract_version="v1",
        binding_attributes=_identity().model_dump(mode="json"),
        mcp_binding_identity=_identity(),
    )
    lookup = build_tool_binding_contract(**common, original_tool_name="lookup")
    renamed = build_tool_binding_contract(**common, original_tool_name="search")
    assert isinstance(lookup, McpToolBindingContract)
    assert isinstance(renamed, McpToolBindingContract)
    assert lookup.binding_fingerprint != renamed.binding_fingerprint
    assert lookup.contract_fact_fingerprint != renamed.contract_fact_fingerprint

    advanced = build_tool_binding_contract(
        **{
            **common,
            "binding_attributes": _identity(2).model_dump(mode="json"),
            "mcp_binding_identity": _identity(2),
        },
        original_tool_name="lookup",
    )
    assert advanced.binding_fingerprint != lookup.binding_fingerprint
    assert advanced.contract_fact_fingerprint != lookup.contract_fact_fingerprint


def test_registry_exactly_joins_tool_and_origin_aware_binding() -> None:
    contract = build_tool_binding_contract(
        tool_name="mcp__docs__lookup",
        origin=ToolBindingOrigin.MCP,
        contract_id="pulsara.mcp.docs.lookup",
        contract_version="v1",
        binding_attributes=_identity().model_dump(mode="json"),
        mcp_binding_identity=_identity(),
        original_tool_name="lookup",
    )
    registry = ToolRegistry()
    registry.register(_Tool(contract.tool_name), binding_contract=contract)
    assert registry.binding_contract(contract.tool_name) is contract
    assert registry.mcp_bindings() == (contract,)

    conflicting = build_tool_binding_contract(
        tool_name=contract.tool_name,
        origin=ToolBindingOrigin.MCP,
        contract_id="pulsara.mcp.docs.lookup",
        contract_version="v1",
        binding_attributes=_identity(2).model_dump(mode="json"),
        mcp_binding_identity=_identity(2),
        original_tool_name="lookup",
    )
    with pytest.raises(ValueError, match="already frozen"):
        registry.bind_contract(conflicting)


def test_mcp_binding_requires_exact_identity_and_original_name() -> None:
    with pytest.raises(ValueError, match="exact identity"):
        build_tool_binding_contract(
            tool_name="mcp__docs__lookup",
            origin=ToolBindingOrigin.MCP,
            contract_id="pulsara.mcp.docs.lookup",
            contract_version="v1",
        )
