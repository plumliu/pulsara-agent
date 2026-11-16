"""Closed capability query branches; business scope is independent of paging."""

from __future__ import annotations

from collections.abc import Mapping
from jsonschema import Draft202012Validator

from pulsara_agent.conversation_kernel.capability_targets import exact_target_path

KINDS = ("SKILL", "HOOK", "MCP", "PLUGIN")
TEXT = {"type": "string", "minLength": 1}
SCOPE = {"type": "string", "enum": ["USER", "WORKSPACE"]}


def _object(fields, required=None):
    return {"type": "object", "properties": fields,
            "required": list(fields) if required is None else required,
            "additionalProperties": False}


def capability_target_schema(kind):
    fields = {
        "PLUGIN": {"plugin_path": TEXT},
        "SKILL": {"skill_path": TEXT},
        "HOOK_SOURCE": {"config_path": TEXT},
        "MCP_CONNECTION": {"config_path": TEXT, "server_id": TEXT},
        "MCP_TOOL": {"server_id": TEXT, "remote_tool_name": TEXT},
        "MCP_RESOURCE": {"server_id": TEXT, "uri": TEXT},
        "MCP_TOOL_REF": {"tool_ref": TEXT},
    }
    if kind not in fields:
        raise ValueError("unsupported public capability Target")
    return _object({"kind": {"const": kind}, **fields[kind]})


def query_input_schema():
    branches = []
    for kind in KINDS:
        filters = {"kind": {"const": kind}, "scope": SCOPE}
        if kind != "PLUGIN":
            filters["parent"] = capability_target_schema("PLUGIN")
        if kind == "MCP":
            filters["query"] = TEXT
        branches.append(_object(filters, ["kind"]))
        targets = {"SKILL": ("SKILL",), "HOOK": ("HOOK_SOURCE",),
                   "PLUGIN": ("PLUGIN",), "MCP": ("MCP_CONNECTION", "MCP_TOOL")}[kind]
        branches.append(_object({"target": {"oneOf": [capability_target_schema(item) for item in targets]},
                                 "kind": {"const": kind}}))
    return {"oneOf": branches}


def _content_schema(kind, value):
    return _object({"type": {"const": kind}, "value": value})


def query_output_schema():
    """The complete query value declared to Cell authors by the SDK guide.

    These are the records produced by the source query owner, including partial
    observations. This declaration is not an execution acceptance gate.
    """
    from pulsara_agent.ports.tool_execution import tool_business_value_schema

    string = {"type": "string"}
    source = {"oneOf": [
        _object({"kind": {"const": "LOCAL"}, "scope": SCOPE}),
        _object({"kind": {"const": "PLUGIN"}, "target": capability_target_schema("PLUGIN")}),
        _object({"kind": {"const": "BUNDLED"}}),
    ]}
    skill = _object({
        "target": capability_target_schema("SKILL"), "name": string,
        "description": string, "source": source, "status": string,
    }, ["target", "source", "status"])
    hook = _object({
        "target": capability_target_schema("HOOK_SOURCE"), "status": string,
        "definitions": {"type": "array", "items": _object({
            "event": string, "matcher": string, "command": string,
        })},
    })
    count = {"type": ["integer", "null"], "minimum": 0}
    plugin = _object({
        "target": capability_target_schema("PLUGIN"), "name": string,
        "description": string, "status": string,
        "components": _object({"skills": count, "hooks": count, "mcp_servers": count}),
    }, ["target", "status", "components"])
    service = _object({
        "target": capability_target_schema("MCP_CONNECTION"), "name": string,
        "status": string, "instructions": string,
        "resource_count": {"type": "integer", "minimum": 0},
        "resource_template_count": {"type": "integer", "minimum": 0},
    }, ["target", "name", "status"])
    tool_directory = _object({
        "target": capability_target_schema("MCP_TOOL"), "description": string,
        "status": string,
    }, ["target", "status"])
    common_detail = {
        "target": capability_target_schema("MCP_TOOL"),
        "description": {"type": "string", "description": "精确查询提供完整描述；关键词查询可能提供描述原文的连续摘录。"},
        "input_schema": {"type": "object", "description": "远端完整参数 JSON Schema；按它构造 call_mcp_tool.arguments。"},
        "output_schema": {"type": "object", "description": "仅在远端实际声明时提供，描述成功 structuredContent，不是整个 Pulsara 返回包。"},
    }
    callable_detail = _object({**common_detail, "invocation": _object({
        "mode": {"const": "META"}, "target": capability_target_schema("MCP_TOOL_REF"),
    })}, ["target", "input_schema", "invocation"])
    unavailable_detail = _object({**common_detail, "invocation": _object({
        "mode": {"const": "UNAVAILABLE"},
    })}, ["target", "invocation"])
    component_target = {"oneOf": [capability_target_schema(kind)
        for kind in ("SKILL", "HOOK_SOURCE", "MCP_CONNECTION")]}
    return tool_business_value_schema(
        content_schema={"type": "array", "items": {"oneOf": [
            _content_schema("text", string),
            _content_schema("target", component_target),
            _content_schema("object", {"oneOf": [skill, hook, plugin, service,
                tool_directory, callable_detail, unavailable_detail]}),
        ]}},
        statuses=("SUCCESS", "ERROR", "CANCELLED"),
    )


def _mcp_text_image_parts():
    return [
        _content_schema("text", {"type": "string"}),
        _content_schema("image", _object({"kind": {"const": "IMAGE_REF"}, "image_ref": TEXT})),
    ]


def call_mcp_tool_output_schema():
    """Formal content is typed; independent remote business JSON stays opaque."""
    from pulsara_agent.ports.tool_execution import tool_business_value_schema

    return tool_business_value_schema(
        fields={"structuredContent": {"description": "远端原样返回的任意 JSON 值；缺失与 null 不同。按实际 output_schema 理解成功值，失败值不假定符合成功 schema。"}},
        content_schema={"type": "array", "items": {"oneOf": _mcp_text_image_parts()}},
        statuses=("SUCCESS", "ERROR", "CANCELLED"),
    )


def read_mcp_resource_output_schema():
    """Connection Targets list records; concrete resource Targets read content."""
    from pulsara_agent.ports.tool_execution import tool_business_value_schema

    string = {"type": "string"}
    resource = _object({
        "target": capability_target_schema("MCP_RESOURCE"), "name": string,
        "description": string, "mime_type": string,
    }, ["target", "name"])
    template = _object({
        "server_id": TEXT, "uri_template": TEXT, "name": string,
        "description": string, "mime_type": string,
    }, ["server_id", "uri_template", "name"])
    return tool_business_value_schema(
        content_schema={"type": "array", "items": {"oneOf": [
            *_mcp_text_image_parts(),
            _content_schema("object", {"oneOf": [resource, template]}),
        ]}},
        statuses=("SUCCESS", "ERROR", "CANCELLED"),
    )


class CapabilityQueryError(ValueError):
    def __init__(self, code, message, *, diagnostics=()):
        super().__init__(message)
        self.code = code
        self.diagnostics = tuple(diagnostics)


def parse_query(arguments: Mapping[str, object], *, subagent=False):
    if not isinstance(arguments, Mapping) or not Draft202012Validator(query_input_schema()).is_valid(dict(arguments)):
        raise CapabilityQueryError("INVALID_ARGUMENTS", "填写必需的 kind；精确查询只填 target、kind，列表使用该类别的可选筛选参数。")
    values = dict(arguments)
    if "query" in values and not values["query"].strip():
        raise CapabilityQueryError("INVALID_ARGUMENTS", "query 必须包含非空关键词。")
    if subagent and values["kind"] != "MCP":
        raise CapabilityQueryError("SOURCE_UNAVAILABLE", "子代理仅查询自身范围内的 MCP 服务和工具。")
    for field in ("target", "parent"):
        target = values.get(field)
        if target:
            for name in ("skill_path", "config_path", "plugin_path"):
                if name in target:
                    exact_target_path(target[name], name)
    return values


__all__ = ["KINDS", "CapabilityQueryError", "capability_target_schema", "query_input_schema",
    "query_output_schema", "call_mcp_tool_output_schema", "read_mcp_resource_output_schema", "parse_query"]
