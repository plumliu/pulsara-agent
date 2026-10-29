"""Cell addresses; liveness and ownership remain with the current Host."""

from collections.abc import Mapping

from pulsara_agent.identity import ID_PAYLOAD_PATTERN, require_id


def cell_target(cell_id: str) -> dict[str, str]:
    return {"kind": "CELL", "cell_id": require_id(cell_id, "cell")}


def parse_cell_target(value: object) -> str:
    if (not isinstance(value, Mapping) or set(value) != {"kind", "cell_id"}
            or value["kind"] != "CELL"):
        raise ValueError("cell target must contain kind CELL and cell_id")
    return require_id(value["cell_id"], "cell")


def cell_target_schema(description: str | None = None) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "const": "CELL"},
            "cell_id": {"type": "string", "pattern": "^cell:" + ID_PAYLOAD_PATTERN + "$"},
        },
        "required": ["kind", "cell_id"],
        "additionalProperties": False,
    }
    if description is not None:
        schema["description"] = description
    return schema
