"""Fixed web inputs and complete public business values shared by native and JS."""

from __future__ import annotations

from pulsara_agent.ports.tool_execution import tool_business_value_schema

WEB_TOOL_NAMES = frozenset({"web_search", "web_fetch"})


def _object(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


def web_url_target_schema() -> dict[str, object]:
    return _object({"kind": {"const": "URL"}, "url": {"type": "string", "minLength": 1,
        "description": "Full HTTP(S) URL without embedded credentials."}}, ("kind", "url"))


def web_parameters_schema(name: str) -> dict[str, object]:
    if name == "web_fetch":
        return _object({"target": web_url_target_schema()}, ("target",))
    if name != "web_search":
        raise ValueError(f"unknown web tool: {name}")
    return _object({
        "query": {"type": "string", "minLength": 1, "pattern": r"\S"},
        "count": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
        "include_domains": {"type": "array", "items": {"type": "string", "minLength": 1},
            "description": "Include these hostnames and their subdomains; omit for unrestricted search."},
        "exclude_domains": {"type": "array", "items": {"type": "string", "minLength": 1},
            "description": "Exclude these hostnames and their subdomains; exclusion takes precedence."},
        "after": {"type": "string", "format": "date", "description": "Search after YYYY-MM-DD."},
        "before": {"type": "string", "format": "date", "description": "Search before YYYY-MM-DD; after must be earlier."},
    }, ("query",))


def web_result_schema(name: str) -> dict[str, object]:
    text = _object({"type": {"const": "text"}, "value": {"type": "string"}}, ("type", "value"))
    if name == "web_search":
        record = _object({"target": web_url_target_schema(),
            **{key: {"type": "string", "minLength": 1} for key in ("title", "snippet", "date")}}, ("target",))
        part = _object({"type": {"const": "object"}, "value": record}, ("type", "value"))
        return tool_business_value_schema(statuses=("SUCCESS", "ERROR", "CANCELLED"),
            content_schema={"type": "array", "items": {"oneOf": [text, part]}})
    if name != "web_fetch":
        raise ValueError(f"unknown web tool: {name}")
    return {"oneOf": [
        tool_business_value_schema(fields={"target": web_url_target_schema()}, required_fields=("target",),
            meta={"title": {"type": "string", "minLength": 1}}),
        tool_business_value_schema(fields={"target": web_url_target_schema()}, statuses=("ERROR", "CANCELLED")),
    ]}
