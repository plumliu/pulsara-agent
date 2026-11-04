"""Schema positions are lowered; parameter names and literal JSON stay intact."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from pulsara_agent.llm.adapters.openai.function_tools import (
    lower_openai_function_parameters,
    openai_chat_function_tool,
    openai_responses_function_tool,
)
from pulsara_agent.llm.input import ToolSpec


@pytest.mark.parametrize(
    "placement", ["root", "array_items", "root_branch", "root_base"]
)
def test_keyword_parameter_names_survive_both_wire_projections(placement):
    names = ["oneOf", "anyOf", "const", "discriminator"]
    schema = {
        "type": "object",
        "properties": {name: {"type": "string", "const": name} for name in names},
        "required": names,
        "additionalProperties": False,
    }
    arguments = {name: name for name in names}
    if placement == "array_items":
        schema = {
            "type": "object",
            "properties": {"values": {"type": "array", "items": schema}},
            "required": ["values"],
            "additionalProperties": False,
        }
        arguments = {"values": [arguments]}
    elif placement == "root_branch":
        schema = {"oneOf": [schema]}
    elif placement == "root_base":
        schema = {**schema, "oneOf": [{"required": names}]}
    original = deepcopy(schema)
    tool = ToolSpec("keyword_parameters", "Accept keyword-named parameters.", schema)
    chat = openai_chat_function_tool(tool)["function"]
    responses = openai_responses_function_tool(tool)
    assert chat == {key: responses[key] for key in chat}
    assert chat["strict"] is False
    assert schema == original
    wire = chat["parameters"]
    Draft202012Validator.check_schema(wire)
    assert Draft202012Validator(schema).is_valid(arguments)
    validator = Draft202012Validator(wire)
    assert validator.is_valid(arguments)
    for name in names:
        wrong = deepcopy(arguments)
        values = wrong["values"][0] if placement == "array_items" else wrong
        values[name] = "wrong"
        assert not validator.is_valid(wrong)
        del values[name]
        assert not validator.is_valid(wrong)
    extra = deepcopy(arguments)
    values = extra["values"][0] if placement == "array_items" else extra
    values["extra"] = "wrong"
    assert not validator.is_valid(extra)


@pytest.mark.parametrize(
    "keyword",
    ["patternProperties", "$defs", "definitions", "dependentSchemas", "dependencies"],
)
def test_schema_mapping_keys_are_not_interpreted_as_keywords(keyword):
    schema = {
        "type": "object",
        keyword: {
            name: {"const": "value"} for name in ["oneOf", "const", "discriminator"]
        },
    }
    lowered = lower_openai_function_parameters(schema)
    assert lowered[keyword] == {name: {"enum": ["value"]} for name in schema[keyword]}
    Draft202012Validator.check_schema(lowered)


@pytest.mark.parametrize("keyword", ["default", "examples", "enum", "x-metadata"])
def test_literal_json_containing_keyword_names_is_preserved(keyword):
    literal = {"oneOf": "data", "anyOf": [], "const": {"discriminator": "data"}}
    value = [literal] if keyword in {"examples", "enum"} else literal
    schema = {"type": "object", keyword: value}
    original = deepcopy(schema)
    lowered = lower_openai_function_parameters(schema)
    assert lowered[keyword] == value
    assert schema == original
    Draft202012Validator.check_schema(lowered)
    # The returned definition must not share mutable literal data with the source.
    result_literal = (
        lowered[keyword][0] if isinstance(value, list) else lowered[keyword]
    )
    result_literal["oneOf"] = "changed"
    assert schema == original


def test_nested_schema_lowering_and_root_union_superset_policy_are_preserved():
    schema = {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "properties": {"action": {"const": "poll"}},
                "required": ["action"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"const": "wait"},
                    "options": {
                        "type": "object",
                        "additionalProperties": {
                            "oneOf": [{"const": 1}, {"const": 2}],
                            "discriminator": {"propertyName": "unused"},
                        },
                    },
                },
                "required": ["action", "options"],
                "additionalProperties": False,
            },
        ],
        "discriminator": {"propertyName": "action"},
    }
    lowered = lower_openai_function_parameters(schema)
    Draft202012Validator.check_schema(lowered)
    assert "oneOf" not in lowered and "discriminator" not in lowered
    assert lowered["properties"]["action"] == {"enum": ["poll", "wait"]}
    assert lowered["properties"]["options"]["additionalProperties"] == {
        "anyOf": [{"enum": [1]}, {"enum": [2]}]
    }
    assert lowered["required"] == ["action"]
    canonical = Draft202012Validator(schema)
    wire = Draft202012Validator(lowered)
    for arguments in [{"action": "poll"}, {"action": "wait", "options": {"const": 1}}]:
        assert canonical.is_valid(arguments)
        assert wire.is_valid(arguments)
    mixed = {"action": "poll", "options": {"const": 1}}
    assert not canonical.is_valid(mixed)
    assert wire.is_valid(mixed)
