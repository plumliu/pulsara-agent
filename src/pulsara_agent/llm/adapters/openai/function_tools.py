"""One provider-neutral OpenAI function-tool wire contract.

OpenAI-compatible endpoints do not all make the same implicit choice when the
``strict`` member is omitted.  In particular, Responses implementations may
attempt Structured Outputs normalization even though Chat Completions treats
the same JSON Schema as non-strict by default.  Pulsara keeps its exact input
schema and validation authority locally, and explicitly requests the OpenAI
documented non-strict function-calling mode on both wire APIs.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pulsara_agent.llm.input import ToolSpec
from pulsara_agent.primitives.context import canonical_json_bytes


OPENAI_FUNCTION_TOOL_WIRE_CONTRACT_VERSION = (
    "v2-explicit-non-strict-prevalidated-lowering"
)


class OpenAIFunctionSchemaIncompatible(ValueError):
    """A canonical object schema has no honest portable wire projection."""


_ROOT_UNION_BRANCH_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "description",
        "title",
        "$comment",
        "deprecated",
        "readOnly",
        "writeOnly",
        "examples",
        "default",
    }
)


def _same_json(left: object, right: object) -> bool:
    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _merge_property_schema(
    left: dict[str, Any], right: dict[str, Any]
) -> dict[str, Any]:
    if _same_json(left, right):
        return left
    left_without_enum = {key: value for key, value in left.items() if key != "enum"}
    right_without_enum = {
        key: value for key, value in right.items() if key != "enum"
    }
    left_enum = left.get("enum")
    right_enum = right.get("enum")
    if (
        isinstance(left_enum, list)
        and isinstance(right_enum, list)
        and _same_json(left_without_enum, right_without_enum)
    ):
        merged = deepcopy(left_without_enum)
        merged["enum"] = []
        for value in (*left_enum, *right_enum):
            if not any(_same_json(value, item) for item in merged["enum"]):
                merged["enum"].append(deepcopy(value))
        return merged

    alternatives: list[dict[str, Any]] = []
    for value in (left, right):
        nested = value.get("anyOf")
        candidates = nested if set(value) == {"anyOf"} and isinstance(nested, list) else [value]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise OpenAIFunctionSchemaIncompatible(
                    "OpenAI function property union is not an object"
                )
            if not any(_same_json(candidate, item) for item in alternatives):
                alternatives.append(deepcopy(candidate))
    return {"anyOf": alternatives}


def _lower_schema_node(value: object) -> object:
    if isinstance(value, list):
        return [_lower_schema_node(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "oneOf" in value and "anyOf" in value:
        raise OpenAIFunctionSchemaIncompatible(
            "OpenAI function schema cannot combine oneOf and anyOf at one node"
        )
    result: dict[str, Any] = {}
    constant_present = "const" in value
    constant = value.get("const")
    for key, nested in value.items():
        if key in {"discriminator", "const"}:
            continue
        if key in {"oneOf", "anyOf"} and (
            not isinstance(nested, list) or not nested
        ):
            raise OpenAIFunctionSchemaIncompatible(
                "OpenAI function schema union must be a non-empty array"
            )
        target_key = "anyOf" if key == "oneOf" else key
        result[target_key] = _lower_schema_node(nested)
    if constant_present:
        existing = result.get("enum")
        if existing is not None and (
            not isinstance(existing, list)
            or not any(_same_json(constant, item) for item in existing)
        ):
            raise OpenAIFunctionSchemaIncompatible(
                "OpenAI function schema const conflicts with enum"
            )
        result["enum"] = [deepcopy(constant)]
    return result


def _root_union_requirement_description(
    *, branches: list[dict[str, Any]], discriminator: str | None
) -> str | None:
    if discriminator is None:
        return None
    clauses: list[str] = []
    for branch in branches:
        properties = branch.get("properties")
        if not isinstance(properties, dict):
            return None
        discriminator_schema = properties.get(discriminator)
        if not isinstance(discriminator_schema, dict):
            return None
        values = discriminator_schema.get("enum")
        if not isinstance(values, list) or len(values) != 1:
            return None
        required = branch.get("required", [])
        if not isinstance(required, list) or any(
            not isinstance(item, str) for item in required
        ):
            return None
        action_requirements = [item for item in required if item != discriminator]
        requirement = ", ".join(action_requirements) if action_requirements else "none"
        clauses.append(
            f"When {discriminator} is {values[0]!r}, required fields: {requirement}."
        )
    return " ".join(clauses)


def _lower_root_object_union(parameters: dict[str, Any]) -> dict[str, Any]:
    raw_one_of = parameters.get("oneOf")
    raw_any_of = parameters.get("anyOf")
    if raw_one_of is not None and raw_any_of is not None:
        raise OpenAIFunctionSchemaIncompatible(
            "OpenAI function root cannot combine oneOf and anyOf"
        )
    raw_branches = raw_one_of if raw_one_of is not None else raw_any_of
    if raw_branches is None:
        lowered = _lower_schema_node(parameters)
        if not isinstance(lowered, dict):  # pragma: no cover - input is closed above
            raise TypeError("OpenAI function schema did not remain an object")
        return lowered
    if not isinstance(raw_branches, list) or not raw_branches:
        raise OpenAIFunctionSchemaIncompatible(
            "OpenAI function root union must have object branches"
        )

    lowered_branches: list[dict[str, Any]] = []
    for raw_branch in raw_branches:
        lowered = _lower_schema_node(raw_branch)
        if not isinstance(lowered, dict) or lowered.get("type") not in {
            None,
            "object",
        }:
            raise OpenAIFunctionSchemaIncompatible(
                "OpenAI function root union branches must constrain objects"
            )
        unsupported_branch_keys = set(lowered).difference(
            _ROOT_UNION_BRANCH_KEYS
        )
        if unsupported_branch_keys:
            raise OpenAIFunctionSchemaIncompatible(
                "OpenAI function root union branch has non-portable constraints"
            )
        if not isinstance(lowered.get("properties", {}), dict):
            raise OpenAIFunctionSchemaIncompatible(
                "OpenAI function root union properties must be an object"
            )
        required = lowered.get("required", [])
        if not isinstance(required, list) or any(
            not isinstance(item, str) for item in required
        ):
            raise OpenAIFunctionSchemaIncompatible(
                "OpenAI function root union required list is invalid"
            )
        additional = lowered.get("additionalProperties", True)
        if not isinstance(additional, bool):
            raise OpenAIFunctionSchemaIncompatible(
                "OpenAI function root union additionalProperties must be boolean"
            )
        lowered_branches.append(lowered)

    base = {
        key: nested
        for key, nested in parameters.items()
        if key
        not in {
            "oneOf",
            "anyOf",
            "discriminator",
            "properties",
            "required",
            "additionalProperties",
        }
    }
    lowered_base = _lower_schema_node(base)
    if not isinstance(lowered_base, dict):  # pragma: no cover - base is an object
        raise TypeError("OpenAI function schema base did not remain an object")
    result: dict[str, Any] = lowered_base
    result["type"] = "object"

    properties: dict[str, Any] = {}
    raw_base_properties = parameters.get("properties", {})
    if not isinstance(raw_base_properties, dict):
        raise OpenAIFunctionSchemaIncompatible(
            "OpenAI function root properties must be an object"
        )
    lowered_base_properties = _lower_schema_node(raw_base_properties)
    if not isinstance(lowered_base_properties, dict):
        raise TypeError("OpenAI function properties did not remain an object")
    properties.update(lowered_base_properties)
    for branch in lowered_branches:
        branch_properties = branch.get("properties", {})
        for name, schema in branch_properties.items():
            if not isinstance(name, str) or not isinstance(schema, dict):
                raise OpenAIFunctionSchemaIncompatible(
                    "OpenAI function property schema is invalid"
                )
            existing = properties.get(name)
            properties[name] = (
                deepcopy(schema)
                if existing is None
                else _merge_property_schema(existing, schema)
            )
    result["properties"] = properties

    base_required = parameters.get("required", [])
    if not isinstance(base_required, list) or any(
        not isinstance(item, str) for item in base_required
    ):
        raise OpenAIFunctionSchemaIncompatible(
            "OpenAI function root required list is invalid"
        )
    common_required = set(lowered_branches[0].get("required", []))
    for branch in lowered_branches[1:]:
        common_required.intersection_update(branch.get("required", []))
    required_set = {*base_required, *common_required}
    result["required"] = [name for name in properties if name in required_set]

    base_additional = parameters.get("additionalProperties", True)
    if not isinstance(base_additional, bool):
        raise OpenAIFunctionSchemaIncompatible(
            "OpenAI function root additionalProperties must be boolean"
        )
    branch_additional = tuple(
        branch.get("additionalProperties", True) for branch in lowered_branches
    )
    # The provider schema may be a superset because Pulsara's immutable local
    # descriptor remains the exact argument authority.  It must never be a
    # narrower set than the canonical MCP/builtin schema: preserve an open
    # root whenever any canonical branch admits unknown properties.
    result["additionalProperties"] = not (
        base_additional is False
        or all(value is False for value in branch_additional)
    )

    raw_discriminator = parameters.get("discriminator")
    discriminator = (
        raw_discriminator.get("propertyName")
        if isinstance(raw_discriminator, dict)
        and isinstance(raw_discriminator.get("propertyName"), str)
        else None
    )
    requirements = _root_union_requirement_description(
        branches=lowered_branches, discriminator=discriminator
    )
    if requirements:
        existing_description = result.get("description")
        result["description"] = (
            f"{existing_description} {requirements}"
            if isinstance(existing_description, str) and existing_description
            else requirements
        )
    return result


def lower_openai_function_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Lower one function schema to OpenAI's portable root-object shape.

    OpenAI's documented Structured Outputs subset permits nested ``anyOf`` but
    forbids a root union.  Root object branches are therefore merged into one
    best-effort provider schema while their exact discriminated-union semantics
    stay in Pulsara's local parser.  No provider name participates in this
    deterministic transformation.
    """

    cloned = deepcopy(parameters)
    declared_type = cloned.get("type")
    if declared_type not in {None, "object"}:
        raise OpenAIFunctionSchemaIncompatible(
            "OpenAI function parameters must have an object root"
        )
    lowered = _lower_root_object_union(cloned)
    lowered.setdefault("type", "object")
    return lowered


def openai_function_definition(tool: ToolSpec) -> dict[str, Any]:
    """Return the shared function definition used by Chat and Responses.

    Function arguments are always JSON objects.  The wire schema may be a
    deterministic superset where OpenAI's portable shape cannot express a
    canonical root union; the immutable local descriptor and strict argument
    parser remain the exact execution truth.
    """

    parameters = lower_openai_function_parameters(tool.parameters)
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": parameters,
        # OpenAI documents that Responses otherwise attempts strict-schema
        # normalization while Chat remains non-strict by default.  Make the
        # shared behavior explicit and deterministic for every function tool.
        "strict": False,
    }


def openai_chat_function_tool(tool: ToolSpec) -> dict[str, Any]:
    return {"type": "function", "function": openai_function_definition(tool)}


def openai_responses_function_tool(tool: ToolSpec) -> dict[str, Any]:
    return {"type": "function", **openai_function_definition(tool)}


__all__ = [
    "OPENAI_FUNCTION_TOOL_WIRE_CONTRACT_VERSION",
    "OpenAIFunctionSchemaIncompatible",
    "lower_openai_function_parameters",
    "openai_chat_function_tool",
    "openai_function_definition",
    "openai_responses_function_tool",
]
