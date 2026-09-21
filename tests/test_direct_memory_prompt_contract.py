"""The model-facing memory guidance explains the direct-write product flow."""

from pulsara_agent.capability.builtin_catalog import builtin_tool_descriptors
from pulsara_agent.ports.system_prompt import DEFAULT_SYSTEM_PROMPT


def test_memory_prompt_explains_write_basis_and_relation_semantics() -> None:
    tools = {item.name: item for item in builtin_tool_descriptors()}
    remember = tools["remember"]
    mark = tools["mark_memory_relation"]

    assert "tool output" in DEFAULT_SYSTEM_PROMPT
    assert "based_on_memory_ids" in DEFAULT_SYSTEM_PROMPT
    assert "Two items saying the same thing" in DEFAULT_SYSTEM_PROMPT
    assert "deleting a true basis also removes its dependents" in DEFAULT_SYSTEM_PROMPT
    assert "A later user correction cannot undo it" in DEFAULT_SYSTEM_PROMPT
    assert "Up to three related items" in DEFAULT_SYSTEM_PROMPT
    assert "memory page" in DEFAULT_SYSTEM_PROMPT

    assert "review is Tuesday" in remember.description
    assert "prepare slides on Monday" in remember.description
    assert "linking the two would be wrong" in remember.description
    assert "This example is illustrative" in remember.description
    assert "ALREADY_PRESENT" in remember.description
    assert "mark_memory_relation" in remember.description
    assert "cited_tool_result_handles" not in remember.input_schema["properties"]
    assert "based_on_memory_ids" in remember.input_schema["properties"]
    assert "CONTRADICTS" in mark.description
    assert "SUPERSEDES" in mark.description
