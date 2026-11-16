"""Project only explicitly emitted, already JSON-frozen Cell values.

QuickJS owns JSON semantics, getters, and serialization failures. This owner
preserves the frozen value's order while finding image content nodes. It never
inspects an unreported program variable or resolves a bare Target.
"""

from __future__ import annotations

import json
from collections.abc import Mapping


def _json(value, *, pretty=False):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )


def project_output_values(values):
    """Return text and unresolved image occurrences, without loading assets."""
    parts, text_chunks = [], []

    def flush_text():
        if text_chunks:
            parts.append({"type":"text", "value":"".join(text_chunks)})
            text_chunks.clear()

    def text(value, *, separate=False):
        if separate:
            flush_text()
        text_chunks.append(value)

    for parameter_index, parameter in enumerate(values):
        if parameter_index:
            text(" ")
        if parameter["type"] == "text":
            text(parameter["value"], separate=True)
            continue
        stack = [("value", parameter["value"], 0)]
        while stack:
            operation, value, depth = stack.pop()
            if operation == "text":
                text(value)
                continue
            if isinstance(value, Mapping):
                kind = value.get("type")
                if kind == "image":
                    flush_text()
                    parts.append({"type": "image", "node": dict(value)})
                    continue
                if (
                    set(value) == {"type", "value"}
                    and kind == "text"
                    and isinstance(value["value"], str)
                ):
                    text(value["value"], separate=True)
                    continue
                if (
                    set(value) == {"type", "value"}
                    and kind == "target"
                    and isinstance(value["value"], Mapping)
                    and isinstance(value["value"].get("kind"), str)
                    and all(
                        not isinstance(item, (Mapping, list))
                        for item in value["value"].values()
                    )
                ):
                    text(_json(value["value"]) + "\n", separate=True)
                    continue
                if (
                    set(value) == {"type", "value"}
                    and kind == "object"
                    and isinstance(value["value"], Mapping)
                ):
                    stack.append(("value", value["value"], depth))
                    continue
                if (
                    set(value) == {"content"}
                    and isinstance(value["content"], list)
                    and all(
                        isinstance(item, Mapping)
                        and item.get("type") in {"text", "image", "target", "object"}
                        for item in value["content"]
                    )
                ):
                    stack.extend(
                        ("value", item, depth) for item in reversed(value["content"])
                    )
                    continue
                items = list(value.items())
                text("{")
                if items:
                    stack.append(("text", "\n" + "  " * depth + "}", depth))
                    for index in range(len(items) - 1, -1, -1):
                        key, item = items[index]
                        if index < len(items) - 1:
                            stack.append(("text", ",", depth))
                        stack.append(("value", item, depth + 1))
                        stack.append(
                            (
                                "text",
                                "\n" + "  " * (depth + 1) + _json(key) + ": ",
                                depth,
                            )
                        )
                else:
                    text("}")
            elif isinstance(value, list):
                text("[")
                if value:
                    stack.append(("text", "\n" + "  " * depth + "]", depth))
                    for index in range(len(value) - 1, -1, -1):
                        if index < len(value) - 1:
                            stack.append(("text", ",", depth))
                        stack.append(("value", value[index], depth + 1))
                        stack.append(("text", "\n" + "  " * (depth + 1), depth))
                else:
                    text("]")
            else:
                text(_json(value))
    flush_text()
    return tuple(parts)


def project_output_record(record):
    kind = record["kind"]
    if kind in {"log", "return"}:
        return kind, project_output_values(record["values"])
    if kind == "error":
        return "diagnostic", project_output_values(
            [{"type": "value", "value": record["error"]}]
        )
    if kind == "tool_result":
        return kind, project_output_values(
            [
                {
                    "type": "text",
                    "value": "未能交给脚本的工具结果（"
                    + record["tool_name"]
                    + "）：\n",
                },
                {"type": "value", "value": record["value"]},
            ]
        )
    raise ValueError("Cell output kind is invalid")
