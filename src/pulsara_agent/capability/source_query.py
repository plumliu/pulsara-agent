"""Closed query inputs and disposable, bounded model projections.

Native owners retain discovery, source validation and execution authority. This
module owns only selection shapes and one response page; targets are not permits.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from jsonschema import Draft202012Validator

from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
)
from pulsara_agent.primitives.tool_result_projection import (
    conservative_artifact_page_logical_utf8_bytes,
)

KINDS = (
    "SKILL",
    "HOOK_SOURCE",
    "MCP_SERVER",
    "MCP_TOOL",
    "MCP_RESOURCE",
    "MCP_RESOURCE_TEMPLATE",
    "MCP_PROMPT",
)
CHILD_KINDS = frozenset(KINDS[3:])
TEXT = {"type": "string", "minLength": 1}
SCOPE = {"type": "string", "enum": ["USER", "WORKSPACE"]}


def _object(fields, required=None):
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields) if required is None else required,
        "additionalProperties": False,
    }


def _target(kind, fields):
    return _object({"kind": {"const": kind}, **fields})


def target_schema(*, parent=False):
    shapes = [_target("PLUGIN", {"scope": SCOPE, "plugin_id": TEXT})]
    for kind in ("MCP_SERVER",) if parent else ("HOOK_SOURCE", "MCP_SERVER"):
        for source in ("LOCAL", "PLUGIN"):
            fields = {"scope": SCOPE, "source_kind": {"const": source}}
            if source == "PLUGIN":
                fields["plugin_id"] = TEXT
            if kind == "MCP_SERVER":
                fields["server_id"] = TEXT
            shapes.append(_target(kind, fields))
    shapes.append(_target("MCP_SERVER", {"runtime_server_id": TEXT}))
    if not parent:
        shapes.append(_target("SKILL", {"skill_path": TEXT}))
        for kind, name in (
            ("MCP_TOOL", "tool_name"),
            ("MCP_RESOURCE", "uri"),
            ("MCP_RESOURCE_TEMPLATE", "uri_template"),
            ("MCP_PROMPT", "name"),
        ):
            shapes.append(_target(kind, {"server_id": TEXT, name: TEXT}))
    return {"oneOf": shapes}


def list_input_schema():
    return _object(
        {
            "kind": {"type": "string", "enum": list(KINDS)},
            "scope": SCOPE,
            "source_kind": {"type": "string", "enum": ["LOCAL", "PLUGIN", "BUNDLED"]},
            "parent": target_schema(parent=True),
            # Existing MCP directory per-page boundary, not a total inventory cap.
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
        [],
    )


def inspect_input_schema():
    return _object({"target": target_schema()})


class CapabilityQueryError(ValueError):
    def __init__(self, code, message, *, diagnostics=()):
        super().__init__(message)
        self.code = code
        self.diagnostics = tuple(diagnostics)


def parse_query(arguments: Mapping[str, object], *, inspect=False, subagent=False):
    schema = inspect_input_schema() if inspect else list_input_schema()
    if not isinstance(arguments, Mapping) or not Draft202012Validator(schema).is_valid(
        dict(arguments)
    ):
        raise CapabilityQueryError(
            "INVALID_ARGUMENTS",
            "Use the closed query fields and copy an exact target; PLUGIN is a source, not a list kind.",
        )
    values = dict(arguments)
    target = values.get("target" if inspect else "parent")
    if (
        target
        and target["kind"] == "SKILL"
        and not Path(target["skill_path"]).is_absolute()
    ):
        raise CapabilityQueryError(
            "INVALID_ARGUMENTS",
            "skill_path must be the absolute path of an observed Skill.",
        )
    if subagent and (
        "scope" in values
        or "source_kind" in values
        or values.get("kind") in {"SKILL", "HOOK_SOURCE"}
        or (
            target
            and (
                target["kind"] in {"PLUGIN", "SKILL", "HOOK_SOURCE"}
                or (
                    target["kind"] == "MCP_SERVER" and "runtime_server_id" not in target
                )
            )
        )
    ):
        raise CapabilityQueryError(
            "ROOT_ONLY",
            "Installation sources can only be observed by the main agent; this caller may query its scoped MCP runtime.",
        )
    if not inspect and target:
        kind = values.get("kind")
        expected = (
            {"SKILL", "HOOK_SOURCE", "MCP_SERVER"}
            if target["kind"] == "PLUGIN"
            else CHILD_KINDS
        )
        if kind is not None and kind not in expected:
            raise CapabilityQueryError(
                "INVALID_ARGUMENTS", "kind is not a child of this parent."
            )
        if target["kind"] == "PLUGIN":
            if (
                values.get("source_kind", "PLUGIN") != "PLUGIN"
                or values.get("scope", target["scope"]) != target["scope"]
            ):
                raise CapabilityQueryError(
                    "INVALID_ARGUMENTS",
                    "The filters conflict with the exact Plugin parent.",
                )
        elif (
            "scope" in target
            and values.get("scope", target["scope"]) != target["scope"]
        ):
            raise CapabilityQueryError(
                "INVALID_ARGUMENTS",
                "scope conflicts with the configured server parent.",
            )
        if (
            target["kind"] == "MCP_SERVER"
            and "source_kind" in target
            and values.get("source_kind", target["source_kind"])
            != target["source_kind"]
        ):
            raise CapabilityQueryError(
                "INVALID_ARGUMENTS",
                "source_kind conflicts with the configured server parent.",
            )
    if (
        not inspect
        and values.get("source_kind") == "BUNDLED"
        and values.get("kind") not in {None, "SKILL"}
    ):
        raise CapabilityQueryError(
            "INVALID_ARGUMENTS", "Bundled sources only provide Skills."
        )
    return values


def query_error(code, message):
    return {"status": code, "message": message}


def fits_result(value):
    return (
        conservative_artifact_page_logical_utf8_bytes(
            tool_call_id="call_" + "x" * 123,
            body=canonical_json_bytes(value).decode("utf-8"),
            model_visible_memory_ids=(),
        )
        <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
    )


def render_page(rows, arguments, *, diagnostics=(), completeness="COMPLETE"):
    # Sort by existing case-sensitive identity, with no snapshot/cursor registry.
    def key(row):
        source = row.get("source") or {}
        return (
            row["kind"],
            source.get("kind", ""),
            source.get("scope", source.get("target", {}).get("scope", "")),
            canonical_json_bytes(row["target"]),
        )

    ordered = sorted(rows, key=key)
    offset, limit = arguments.get("offset", 0), arguments.get("limit", 50)
    maximum = min(limit, max(0, len(ordered) - offset))
    for count in range(maximum, -1, -1):
        end = offset + count
        value = {
            "items": ordered[offset:end],
            "returned_count": count,
            "total_count": len(ordered),
            "truncated": end < len(ordered),
            "next_offset": end if end < len(ordered) else None,
            "completeness": completeness,
            "diagnostics": list(diagnostics),
        }
        if fits_result(value):
            if count == 0 and offset < len(ordered):
                raise CapabilityQueryError(
                    "CAPABILITY_ROW_OVERBOUND",
                    "One capability row exceeds the existing tool result budget.",
                )
            return value
    raise CapabilityQueryError(
        "CAPABILITY_RESULT_OVERBOUND",
        "Query diagnostics exceed the existing tool result budget.",
    )


def minimal_row(kind, name, target, source, status, description=None):
    row = {
        "kind": kind,
        "name": name,
        "target": target,
        "source": source,
        "status": status,
    }
    if description:
        row["description"] = description
    return row


def plugin_target(item):
    return {
        "kind": "PLUGIN",
        "scope": item.identity.scope.value,
        "plugin_id": item.identity.plugin_id,
    }


def plugin_source(item):
    return {"kind": "PLUGIN", "target": plugin_target(item)}


def public_diagnostics(items):
    """Explicit projection; native diagnostic DTOs are never serialized wholesale."""
    result = []
    for item in items:
        nested = getattr(item, "diagnostics", None)
        if nested is not None:
            result.extend(public_diagnostics(nested))
            continue
        code = getattr(item, "code", None)
        message = getattr(item, "message", None)
        if code is None or message is None:
            continue
        value = {"code": getattr(code, "value", code), "message": message}
        for name in ("component", "path"):
            field = getattr(item, name, None)
            if field is not None:
                value[name] = str(getattr(field, "value", field))
        if value not in result:
            result.append(value)
    return result
