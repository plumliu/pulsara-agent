"""Pure current native memory parameter contracts."""

from __future__ import annotations


def memory_input_schema(name: str) -> dict[str, object]:
    """The three current native memory signatures; runtime normalizes text."""
    from pulsara_agent.memory.targets import memory_target_schema

    context = {"type": "string", "enum": ["GLOBAL", "CURRENT_PROJECT"]}
    target = memory_target_schema()

    def object_schema(properties, required):
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    if name == "remember":
        schema = object_schema(
            {
                "context_kind": context,
                "kind": {
                    "type": "string",
                    "enum": ["USER_PROFILE", "RESPONSE_PREFERENCE", "FACT"],
                },
                "title": {
                    "type": "string",
                    "description": "Immutable nonempty single-line title; normalized NFC, at most 128 Unicode characters. For RESPONSE_PREFERENCE this is the complete preference.",
                },
                "statement": {
                    "type": "string",
                    "description": "USER_PROFILE/FACT require a nonempty normalized body of at most 8000 Unicode characters. RESPONSE_PREFERENCE is always saved with an empty body.",
                },
            },
            ["context_kind", "kind", "title"],
        )
        schema["anyOf"] = [
            {
                "properties": {"kind": {"enum": ["USER_PROFILE", "FACT"]}},
                "required": ["statement"],
            },
            {"properties": {"kind": {"const": "RESPONSE_PREFERENCE"}}},
        ]
        return schema
    if name == "query_memory":
        return {
            "oneOf": [
                object_schema({"target": target}, ["target"]),
                object_schema(
                    {
                        "query": {"type": "string", "minLength": 1},
                        "limit": {"type": "integer", "minimum": 1, "default": 5},
                    },
                    ["query"],
                ),
            ]
        }
    if name == "manage_memory":
        base = {"target": target, "action": {"const": "edit"}}
        return {
            "oneOf": [
                object_schema(
                    {**base, "statement": {"type": "string"}},
                    ["target", "action", "statement"],
                ),
                object_schema(
                    {**base, "context_kind": context},
                    ["target", "action", "context_kind"],
                ),
                object_schema(
                    {**base, "statement": {"type": "string"}, "context_kind": context},
                    ["target", "action", "statement", "context_kind"],
                ),
                object_schema(
                    {"target": target, "action": {"const": "delete"}},
                    ["target", "action"],
                ),
            ]
        }
    raise KeyError(name)
