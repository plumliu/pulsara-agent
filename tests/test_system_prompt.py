from pulsara_agent.ports.system_prompt import DEFAULT_SYSTEM_PROMPT


def test_default_system_prompt_is_pulsara_native_and_capability_honest() -> None:
    assert DEFAULT_SYSTEM_PROMPT.startswith(
        "You are Pulsara, an agentic coding runtime"
    )
    assert "Inspection, explanation, diagnosis, and review are read-only" in (
        DEFAULT_SYSTEM_PROMPT
    )
    assert "Tool visibility is not authorization" in DEFAULT_SYSTEM_PROMPT
    assert "Never claim to have inspected, changed, run, or verified" in (
        DEFAULT_SYSTEM_PROMPT
    )
    assert "Memory is advisory, may be stale or incomplete" in DEFAULT_SYSTEM_PROMPT
    assert "announced in the MCP catalog under new_tool_names" in DEFAULT_SYSTEM_PROMPT
    assert "route is NEW_MCP_META_ONLY" in DEFAULT_SYSTEM_PROMPT
    assert "new_tool_names in list_mcp_servers" not in DEFAULT_SYSTEM_PROMPT

    for foreign_product_contract in (
        "Claude",
        "Anthropic",
        "web_search",
        "end_conversation",
        "/home/claude",
        "persistent_storage",
    ):
        assert foreign_product_contract not in DEFAULT_SYSTEM_PROMPT
