"""MCP search ranking over a frozen, already scoped directory."""

from types import SimpleNamespace

from pulsara_agent.capability.mcp_search import mcp_tool_search_text, search_mcp


def _tool(name, description, *, server_id="tools"):
    return SimpleNamespace(server_id=server_id, remote_tool_name=name, description=description)


def _server(name, instructions):
    return SimpleNamespace(server_id=name, display_name=name, sanitized_instructions=instructions)


def test_tool_search_text_preserves_public_text_and_excludes_schema():
    tool = _tool("find_pages", "  Find team pages.\nSee examples.  ")
    tool.input_schema = {"description": "sphinx"}
    tool.output_schema = {"description": "quasar"}

    assert mcp_tool_search_text(tool) == "find_pages\n  Find team pages.\nSee examples.  "
    assert mcp_tool_search_text(_tool("find_pages", None)) == "find_pages\n"
    assert search_mcp("sphinx", tools=[tool], servers=[]) == []
    assert search_mcp("quasar", tools=[tool], servers=[]) == []


def test_equal_rrf_scores_break_ties_by_directory_after_service_hits_are_removed():
    sparse_one = _tool("alpha", "keyword")
    sparse_two = _tool("beta", "keyword extra words")
    dense_one = _tool("gamma", "Reproduce an existing page.")
    dense_two = _tool("delta", "Duplicate a document.")
    both = _tool("epsilon", "keyword with a rather longer description")
    service = _server("keyword", "keyword")
    directory = [sparse_two, dense_one, both, dense_two, sparse_one]
    dense_similarities = {
        ("tools", "gamma"): 0.90,
        ("tools", "delta"): 0.80,
        ("tools", "epsilon"): 0.70,
    }

    matches = search_mcp("keyword", tools=directory, servers=[service])
    assert [item for _, item, _ in matches] == [sparse_one, sparse_two, both, service]

    matches = search_mcp(
        "keyword", tools=directory, servers=[service], dense_similarities=dense_similarities
    )

    # Rank three on both lists outranks rank one on either list alone.
    assert [item for _, item, _ in matches] == [both, dense_one, sparse_one, sparse_two, dense_two, service]
    assert matches[1] == ("tool", dense_one, "Reproduce an existing page.")
    assert matches[-1] == ("service", service, None)


def test_exact_name_group_preserves_separate_tools_and_directory_order():
    first = _tool("quasar", "An option with a much longer description.", server_id="one")
    second = _tool("quasar", "quasar", server_id="two")
    both = _tool("alternative", "quasar")
    directory = [both, first, second]

    hits = search_mcp("quasar", tools=directory, servers=[], dense_similarities={
        ("tools", "alternative"): 0.95,
        ("two", "quasar"): 0.90,
    })

    assert [tool for _, tool, _ in hits] == [first, second, both]


def test_dense_cosine_admission_preserves_no_match_and_stable_similarity_ties():
    first = _tool("copy", "Create a duplicate.")
    second = _tool("duplicate", "Clone this page.")
    low = _tool("delete", "Remove an item.")
    bad = _tool("update", "Edit an item.")
    directory = [first, second, low, bad]
    # 0.50 is the provisional MCP operating default pending calibration on tool samples.
    similarities = {
        ("tools", "duplicate"): 0.50,
        ("tools", "copy"): 0.50,
        ("tools", "delete"): 0.49,
        ("tools", "update"): float("nan"),
        ("invisible", "copy"): 0.99,
    }
    hits = search_mcp("reproduce", tools=directory, servers=[], dense_similarities=similarities)
    assert [tool for _, tool, _ in hits] == [first, second]
    assert search_mcp("unrelated", tools=directory, servers=[], dense_similarities={
        ("tools", "delete"): 0.49,
        ("tools", "update"): float("inf"),
    }) == []


def test_exact_name_handles_directory_without_lexical_tokens():
    tool = _tool("💡", None)
    assert search_mcp("💡", tools=[tool], servers=[]) == [("tool", tool, None)]
    assert search_mcp("hint", tools=[tool], servers=[]) == []
