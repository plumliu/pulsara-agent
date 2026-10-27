"""Public memory object addresses; storage and visibility remain with their owners."""

from collections.abc import Mapping

from pulsara_agent.identity import require_id


def memory_target(memory_id: str) -> dict[str, str]:
    return {"kind": "MEMORY", "memory_id": memory_id}


def parse_memory_target(value: object) -> str:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"kind", "memory_id"}
        or value["kind"] != "MEMORY"
    ):
        raise ValueError("memory target must contain kind MEMORY and memory_id")
    return require_id(value["memory_id"], "memory")


def memory_target_schema(description: str | None = None) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "const": "MEMORY"},
            "memory_id": {"type": "string", "minLength": 1},
        },
        "required": ["kind", "memory_id"],
        "additionalProperties": False,
    }
    if description is not None:
        schema["description"] = description
    return schema
