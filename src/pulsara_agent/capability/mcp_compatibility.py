"""Finite MCP input-contract widening rules, not a JSON Schema interpreter.

referencing owns schema-node traversal (including dialect-specific containers).
Only simple, reference-free schemas use the structural widening rules below;
complex contracts can still retain native execution when only prose changed.
"""

from enum import StrEnum

from referencing import Resource
from referencing.jsonschema import DRAFT202012

from pulsara_agent.model_input.contracts import FrozenToolSpec
from pulsara_agent.primitives.context import canonical_json_bytes, thaw_json


class NativeMcpCompatibility(StrEnum):
    COMPATIBLE = "COMPATIBLE"
    INPUT_CHANGED = "INPUT_CHANGED"
    OUTPUT_CHANGED = "OUTPUT_CHANGED"
    UNCONFIRMED = "UNCONFIRMED"


_PROSE = frozenset({"description", "title", "$comment", "examples"})
_LOWER = frozenset(
    {"minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties"}
)
_UPPER = frozenset(
    {"maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties"}
)
_SIMPLE = (
    _PROSE
    | _LOWER
    | _UPPER
    | {
        "$schema",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "pattern",
        "format",
        "uniqueItems",
        "multipleOf",
        "default",
        "deprecated",
        "readOnly",
        "writeOnly",
    }
)


def _same(left, right):
    # Python equality conflates JSON booleans and numbers.
    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _nodes(schema):
    pending = [Resource.from_contents(schema, default_specification=DRAFT202012)]
    seen = set()
    while pending:
        resource = pending.pop()
        if id(resource.contents) in seen:
            continue
        seen.add(id(resource.contents))
        yield resource.contents
        pending.extend(resource.subresources())


def _without_prose(schema):
    nodes = {id(value) for value in _nodes(schema) if isinstance(value, dict)}

    def clean(value):
        if isinstance(value, dict):
            is_schema = id(value) in nodes
            result = {}
            for key, child in value.items():
                if is_schema and key in _PROSE:
                    continue
                if (
                    is_schema
                    and key in {"enum", "required"}
                    and isinstance(child, list)
                ):
                    result[key] = sorted(
                        (clean(item) for item in child), key=canonical_json_bytes
                    )
                else:
                    result[key] = clean(child)
            return result
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return clean(schema)


def _simple(schema):
    return all(
        not isinstance(node, dict) or set(node) <= _SIMPLE for node in _nodes(schema)
    )


def _widens(old, new):
    if _same(old, new):
        return True
    if (old is True and _same(new, {})) or (new is True and _same(old, {})):
        return True
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    handled = (
        {"properties", "required", "additionalProperties", "items", "enum", "const"}
        | _LOWER
        | _UPPER
    )
    if not _same(
        {k: v for k, v in old.items() if k not in handled},
        {k: v for k, v in new.items() if k not in handled},
    ):
        return False
    if not set(new.get("required", ())) <= set(old.get("required", ())):
        return False
    old_properties, new_properties = (
        old.get("properties", {}),
        new.get("properties", {}),
    )
    if not set(old_properties) <= set(new_properties):
        return False
    if any(
        not _widens(value, new_properties[key]) for key, value in old_properties.items()
    ):
        return False
    old_extra, new_extra = (
        old.get("additionalProperties", True),
        new.get("additionalProperties", True),
    )
    if not (old_extra is False or new_extra is True or _same(old_extra, new_extra)):
        return False
    for key in set(new_properties) - set(old_properties):
        if old_extra is not False and not _widens(old_extra, new_properties[key]):
            return False
    if "items" in old or "items" in new:
        if not _widens(old.get("items", True), new.get("items", True)):
            return False
    if "const" in new:
        if "const" not in old or not _same(old["const"], new["const"]):
            return False
    if "const" in old and "const" not in new:
        if "enum" not in new or not any(
            _same(old["const"], value) for value in new["enum"]
        ):
            return False
    if "enum" in new:
        values = [old["const"]] if "const" in old else old.get("enum")
        if values is None or any(
            not any(_same(value, item) for item in new["enum"]) for value in values
        ):
            return False
    for key in _LOWER | _UPPER:
        if key not in new or _same(old.get(key), new[key]):
            continue
        if key not in old or isinstance(old[key], bool) or isinstance(new[key], bool):
            return False
        if (key in _LOWER and new[key] > old[key]) or (
            key in _UPPER and new[key] < old[key]
        ):
            return False
    return True


def _unconfirmed_change(old, new):
    # These fields can affect execution or express constraints for which this
    # finite classifier does not prove inclusion. Never label them as prose.
    uncertain = {
        "$schema",
        "pattern",
        "format",
        "multipleOf",
        "default",
        "deprecated",
        "readOnly",
        "writeOnly",
    }
    before = old if isinstance(old, dict) else {}
    after = new if isinstance(new, dict) else {}
    if not _same(
        {key: value for key, value in before.items() if key in uncertain},
        {key: value for key, value in after.items() if key in uncertain},
    ):
        return True
    old_properties = before.get("properties", {})
    new_properties = after.get("properties", {})
    return (
        any(
            _unconfirmed_change(old_properties[key], new_properties[key])
            for key in old_properties.keys() & new_properties.keys()
        )
        or any(
            _unconfirmed_change(
                before.get("additionalProperties", True), new_properties[key]
            )
            for key in new_properties.keys() - old_properties.keys()
            if before.get("additionalProperties", True) is not False
        )
        or any(
            _unconfirmed_change(before.get(key, True), after.get(key, True))
            for key in ("items", "additionalProperties")
            if key in before or key in after
        )
    )


def native_mcp_compatibility(
    old: FrozenToolSpec, new: FrozenToolSpec
) -> NativeMcpCompatibility:
    old_output = (
        None
        if old.output_schema is None
        else _without_prose(thaw_json(old.output_schema))
    )
    new_output = (
        None
        if new.output_schema is None
        else _without_prose(thaw_json(new.output_schema))
    )
    if not _same(old_output, new_output):
        return NativeMcpCompatibility.OUTPUT_CHANGED
    before, after = (
        _without_prose(thaw_json(old.parameters)),
        _without_prose(thaw_json(new.parameters)),
    )
    if _same(before, after):
        return NativeMcpCompatibility.COMPATIBLE
    if not (_simple(before) and _simple(after)) or _unconfirmed_change(before, after):
        return NativeMcpCompatibility.UNCONFIRMED
    return (
        NativeMcpCompatibility.COMPATIBLE
        if _widens(before, after)
        else NativeMcpCompatibility.INPUT_CHANGED
    )
