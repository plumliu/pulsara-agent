"""The model memory surface has one point read and no source-audit tool."""

import pytest

from pulsara_agent.capability.builtin_catalog import (
    builtin_tool_catalog_entry,
    builtin_tool_descriptors,
)
from pulsara_agent.conversation_kernel.memory_tools import MEMORY_TOOL_NAMES


def test_memory_get_is_the_only_point_read_without_source_options():
    descriptors = {
        item.name: item for item in builtin_tool_descriptors()
        if item.name.startswith("memory_") or item.name in {"remember", "mark_memory_relation"}
    }
    assert set(descriptors) == MEMORY_TOOL_NAMES == {
        "memory_search", "memory_get", "remember", "mark_memory_relation",
    }
    with pytest.raises(KeyError, match="unknown builtin tool catalog entry"):
        builtin_tool_catalog_entry("memory_explain")
    descriptor = descriptors["memory_get"]
    assert set(descriptor.input_schema["properties"]) == {"memory_id"}
    assert descriptor.input_schema["required"] == ("memory_id",)
    assert descriptor.input_schema["additionalProperties"] is False
    assert descriptor.is_read_only is True
