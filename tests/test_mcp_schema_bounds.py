from dataclasses import replace

import pytest

from pulsara_agent.conversation_kernel.mcp.wire import (
    DEFAULT_MCP_WIRE_BOUNDS,
    McpSchemaBoundExceeded,
    validate_schema,
)


def test_ordinary_schema_above_old_4096_node_limit_is_admitted():
    # Regression for real Notion discovery: 5770 nodes / 64 KB, not an
    # exceptional provider profile. Field names count as nodes too.
    schema = {"type": "object", "properties": {
        f"field_{i}": {"type": "string", "description": "A field"}
        for i in range(1000)
    }}
    validate_schema(schema, DEFAULT_MCP_WIRE_BOUNDS)


def test_schema_reuses_wire_node_budget_without_a_second_setting():
    assert not hasattr(DEFAULT_MCP_WIRE_BOUNDS, "maximum_schema_nodes")
    assert not hasattr(DEFAULT_MCP_WIRE_BOUNDS, "maximum_sse_event_data_bytes")
    assert not hasattr(DEFAULT_MCP_WIRE_BOUNDS, "maximum_buffered_transport_bytes_per_slot")
    with pytest.raises(McpSchemaBoundExceeded, match="node"):
        validate_schema({"type": "object"}, replace(
            DEFAULT_MCP_WIRE_BOUNDS, maximum_wire_json_nodes=2,
        ))


def test_schema_byte_and_depth_admission_are_preserved():
    with pytest.raises(McpSchemaBoundExceeded, match="byte"):
        validate_schema({"description": "x" * DEFAULT_MCP_WIRE_BOUNDS.maximum_schema_utf8_bytes}, DEFAULT_MCP_WIRE_BOUNDS)
    nested = {}
    for _ in range(DEFAULT_MCP_WIRE_BOUNDS.maximum_schema_depth):
        nested = {"nested": nested}
    with pytest.raises(McpSchemaBoundExceeded, match="depth"):
        validate_schema(nested, DEFAULT_MCP_WIRE_BOUNDS)
