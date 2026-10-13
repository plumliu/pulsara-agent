"""Memory tool schemas expose direct-write and relation contracts."""

from pulsara_agent.capability.builtin_catalog import builtin_tool_descriptors


def test_memory_tools_expose_direct_write_basis_and_relation_kinds() -> None:
    tools = {item.name: item for item in builtin_tool_descriptors()}
    remember = tools["remember"].input_schema
    assert "cited_tool_result_handles" not in remember["properties"]
    assert "based_on_memory_ids" in remember["properties"]
    relation = tools["mark_memory_relation"].input_schema
    assert set(relation["properties"]["relation_kind"]["enum"]) == {
        "CONTRADICTS", "SUPERSEDES"
    }
