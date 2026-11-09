"""MCP standard-operation targets and resource URI template matching."""

from __future__ import annotations

import re
from typing import Mapping

from .contracts import McpDiscoverySnapshot

_URI_TEMPLATE_VARIABLE = re.compile(
    r"^[A-Za-z0-9_.%]+(?::[1-9][0-9]{0,3}|\*)?$"
)


def _standard_operation_target(tool_name, arguments):
    target = arguments.get("target")
    if not isinstance(target, Mapping):
        raise ValueError("MCP standard operation requires a target")
    kind = target.get("kind")
    if tool_name == "read_mcp_resource" and kind in {"MCP_RESOURCE", "MCP_RESOURCE_TEMPLATE"}:
        identity = "uri" if kind == "MCP_RESOURCE" else "uri_template"
        allowed = {"target"} if kind == "MCP_RESOURCE" else {"target", "uri"}
        if set(arguments) != allowed:
            raise ValueError("MCP resource fields do not match its target kind")
    else:
        raise ValueError("MCP standard target kind is unsupported")
    if set(target) != {"kind", "server_id", identity}:
        raise ValueError("MCP standard target fields are incomplete or unsupported")
    for identity_field, maximum in (("server_id", 256), (identity, 8192 if identity == "name" else 32768)):
        value = target[identity_field]
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise ValueError("MCP standard target identity is invalid")
    return target


def _standard_resource_uri(arguments, snapshot):
    target = _standard_operation_target("read_mcp_resource", arguments)
    if target["kind"] == "MCP_RESOURCE":
        uri = target["uri"]
        admitted = _resource_uri_in_snapshot(uri, snapshot)
    else:
        uri = arguments["uri"]
        template = target["uri_template"]
        admitted = (
            isinstance(uri, str)
            and any(item.uri_template == template for item in snapshot.resource_templates)
            and _resource_uri_matches_template(uri, template)
        )
    if not admitted:
        raise ValueError("MCP resource URI is absent from the exact snapshot or selected template")
    return uri


def _resource_uri_in_snapshot(
    uri: str, snapshot: McpDiscoverySnapshot
) -> bool:
    """Join a resource URI to one exact static entry or RFC6570 template.

    This is an admission check, not a second resource authority: the template
    itself remains part of the immutable discovery generation and the remote
    server still decides whether the concrete resource exists.  Unsupported or
    malformed template syntax stays visible as catalog metadata but cannot
    authorize an open-ended read.
    """

    try:
        encoded = uri.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if not encoded or len(encoded) > 32 * 1024 or any(ord(char) < 0x20 for char in uri):
        return False
    if uri in {item.uri for item in snapshot.resources}:
        return True
    return any(
        _resource_uri_matches_template(uri, item.uri_template)
        for item in snapshot.resource_templates
    )


def _resource_uri_matches_template(uri: str, template: str) -> bool:
    """Linearly match a conservative, bounded RFC6570 subset.

    Regex wildcards are deliberately absent: each literal is consumed once and
    each expansion is validated once.  Adjacent expressions are rejected
    because their split is not uniquely provable without backtracking.  Named
    query and matrix expansions admit only their advertised variable names.
    """

    try:
        if (
            not uri
            or len(uri.encode("utf-8")) > 32 * 1024
            or len(template.encode("utf-8")) > 32 * 1024
        ):
            return False
    except UnicodeEncodeError:
        return False
    if any(ord(character) < 0x20 for character in uri + template):
        return False
    parsed = _parse_resource_uri_template(template)
    if parsed is None:
        return False
    expressions, tail = parsed
    position = 0
    for index, (literal, operator, variables) in enumerate(expressions):
        if not uri.startswith(literal, position):
            return False
        position += len(literal)
        next_literal = (
            expressions[index + 1][0]
            if index + 1 < len(expressions)
            else tail
        )
        if next_literal:
            end = uri.find(next_literal, position)
            if end < 0:
                return False
        else:
            end = len(uri)
        if not _match_uri_template_expansion(
            uri[position:end], operator=operator, variables=variables
        ):
            return False
        position = end
    return uri.startswith(tail, position) and position + len(tail) == len(uri)


def _parse_resource_uri_template(
    template: str,
) -> tuple[tuple[tuple[str, str, tuple[str, ...]], ...], str] | None:
    expressions: list[tuple[str, str, tuple[str, ...]]] = []
    cursor = 0
    while cursor < len(template):
        opening = template.find("{", cursor)
        closing_without_open = template.find("}", cursor)
        if opening < 0:
            if closing_without_open >= 0:
                return None
            break
        if closing_without_open >= 0 and closing_without_open < opening:
            return None
        literal = template[cursor:opening]
        # Without a literal separator there is no unique linear split between
        # two expansions, so the conservative admission answer is false.
        if expressions and not literal:
            return None
        closing = template.find("}", opening + 1)
        if closing < 0 or "{" in template[opening + 1 : closing]:
            return None
        expression = template[opening + 1 : closing]
        if not expression or len(expressions) >= 128:
            return None
        operator = expression[0] if expression[0] in "+#./;?&" else ""
        variable_list = expression[1:] if operator else expression
        variables = tuple(variable_list.split(","))
        if (
            not variables
            or len(variables) > 128
            or any(
                _URI_TEMPLATE_VARIABLE.fullmatch(variable) is None
                for variable in variables
            )
        ):
            return None
        expressions.append((literal, operator, variables))
        cursor = closing + 1
    tail = template[cursor:]
    if "{" in tail or "}" in tail:
        return None
    return tuple(expressions), tail


def _match_uri_template_expansion(
    expansion: str,
    *,
    operator: str,
    variables: tuple[str, ...],
) -> bool:
    names = tuple(_uri_template_variable_name(variable) for variable in variables)
    exploded = {
        _uri_template_variable_name(variable)
        for variable in variables
        if variable.endswith("*")
    }
    if operator in {"?", "&"}:
        return _match_named_uri_expansion(
            expansion,
            prefix=operator,
            separator="&",
            names=names,
            exploded=exploded,
        )
    if operator == ";":
        return _match_named_uri_expansion(
            expansion,
            prefix=";",
            separator=";",
            names=names,
            exploded=exploded,
        )
    if not expansion:
        return True
    if operator == "+":
        return "#" not in expansion
    if operator == "#":
        return expansion.startswith("#") and "#" not in expansion[1:]
    if operator in {".", "/"}:
        if not expansion.startswith(operator) or any(
            character in "?#" for character in expansion
        ):
            return False
        values = expansion[1:].split(operator)
        return all(values) and (
            len(values) <= len(variables) or bool(exploded)
        )
    if any(character in "?#" for character in expansion):
        return False
    if exploded:
        # A simple exploded variable is used by common resource templates for
        # a path tail.  It may cross '/', but never query or fragment syntax.
        return True
    if any(character in ":/" for character in expansion):
        return False
    values = expansion.split(",")
    return len(values) <= len(variables)


def _match_named_uri_expansion(
    expansion: str,
    *,
    prefix: str,
    separator: str,
    names: tuple[str, ...],
    exploded: set[str],
) -> bool:
    if not expansion:
        return True
    if not expansion.startswith(prefix) or "#" in expansion:
        return False
    fields = expansion[1:].split(separator)
    if not fields or any(not field for field in fields):
        return False
    seen: set[str] = set()
    allowed = set(names)
    for parameter in fields:
        name, _separator_found, _value = parameter.partition("=")
        if not name or name not in allowed:
            return False
        if name in seen and name not in exploded:
            return False
        # Matrix parameters may omit '=value'; query parameters may too.  In
        # both cases admission depends on the exact advertised key, not value.
        seen.add(name)
    return True


def _uri_template_variable_name(variable: str) -> str:
    raw = variable[:-1] if variable.endswith("*") else variable
    return raw.split(":", 1)[0]
