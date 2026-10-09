"""The model-facing memory guidance explains the direct-write product flow."""

from pulsara_agent.capability.builtin_catalog import builtin_tool_descriptors
from pulsara_agent.ports.system_prompt import DEFAULT_SYSTEM_PROMPT
from pulsara_agent.conversation_kernel.memory.contracts import (
    MAXIMUM_MEMORY_STATEMENT_BYTES,
    MAXIMUM_RESPONSE_PREFERENCE_STATEMENT_BYTES,
)


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

    assert "decision to prepare slides before a review" in remember.description
    assert "rewording that schedule is not a new dependent memory" in remember.description
    assert "Examples are not content to save" in remember.description
    assert "ALREADY_PRESENT" in remember.description
    assert "mark_memory_relation" in remember.description
    assert "cited_tool_result_handles" not in remember.input_schema["properties"]
    assert "based_on_memory_ids" in remember.input_schema["properties"]
    assert "CONTRADICTS" in mark.description
    assert "SUPERSEDES" in mark.description


def test_remember_statement_description_explains_kind_specific_byte_limits() -> None:
    remember = next(item for item in builtin_tool_descriptors() if item.name == "remember")
    description = remember.input_schema["properties"]["statement"]["description"]
    assert f"at most {MAXIMUM_MEMORY_STATEMENT_BYTES} UTF-8 bytes" in description
    assert f"({MAXIMUM_RESPONSE_PREFERENCE_STATEMENT_BYTES} for RESPONSE_PREFERENCE)" in description


def test_memory_prompts_preserve_attribution_and_relation_conditions() -> None:
    tools = {item.name: item for item in builtin_tool_descriptors()}
    statement = tools["remember"].input_schema["properties"]["statement"]["description"]
    for text in (statement, DEFAULT_SYSTEM_PROMPT):
        assert "suggestion, tentative plan, adopted choice, and completed action" in text
        assert "preserve who said or did it" in text
        assert "Resolve pronouns and relative dates only" in text
    relation = tools["mark_memory_relation"]
    for text in (
        DEFAULT_SYSTEM_PROMPT,
        relation.description,
        relation.input_schema["properties"]["relation_kind"]["description"],
    ):
        assert "same subject under the same conditions" in text
        assert "Different dates or conditions may explain a difference" in text
        assert "A later save time alone does not justify SUPERSEDES" in text
    query = tools["memory_search"].input_schema["properties"]["query"]["description"]
    assert "known project and distinguishing details" in query
    assert "without guessing missing facts" in query
