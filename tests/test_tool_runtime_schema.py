"""Argument diagnostics use the existing validator's resolved schema and paths."""
from copy import deepcopy

import pytest
from jsonschema import ValidationError

from pulsara_agent.conversation_kernel.mcp.schema import validate_mcp_arguments
from pulsara_agent.conversation_kernel.tool_runtime_schema import _tool_argument_error_message


def test_mcp_parameter_suggestion_uses_local_reference_and_nested_path():
    schema = {
        "$defs": {"item": {
            "type": "object", "properties": {"limit": {"type": "integer"}},
            "additionalProperties": False,
        }},
        "type": "object", "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/item"}}},
    }
    arguments = {"items": [{"limti": 3}]}
    original = deepcopy(arguments)
    with pytest.raises(ValidationError) as rejected:
        validate_mcp_arguments(schema, arguments)
    message = _tool_argument_error_message(rejected.value)
    assert "At $.items[0]: unknown parameter 'limti'. Did you mean 'limit'?" in message
    assert arguments == original


def test_valid_pattern_property_does_not_receive_a_typo_suggestion():
    schema = {
        "type": "object", "properties": {"limit": {"type": "integer"}},
        "patternProperties": {"^limits_": {"type": "integer"}}, "additionalProperties": False,
    }
    with pytest.raises(ValidationError) as rejected:
        validate_mcp_arguments(schema, {"limits_a": 3, "completely_unrelated": 4})
    message = _tool_argument_error_message(rejected.value)
    assert "Did you mean" not in message


def test_type_error_keeps_validator_detail_and_parameter_location():
    schema = {"type": "object", "properties": {"limit": {"type": "integer"}}}
    with pytest.raises(ValidationError) as rejected:
        validate_mcp_arguments(schema, {"limit": "ten"})
    message = _tool_argument_error_message(rejected.value)
    assert message == "$.limit: 'ten' is not of type 'integer'"
