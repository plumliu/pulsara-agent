"""Immutable, finite connection-input definitions for converted Plugin packages.

This is a declaration parser, not an expression evaluator or a values registry.
Instance values remain in connection overlays and the existing private settings.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from pulsara_agent.mcp_credentials import (
    BoundSecretValue,
    ManagedLocalCredentialReference,
    McpCredentialBinding,
)


@dataclass(frozen=True, slots=True)
class ConnectionInput:
    name: str
    title: str
    private: bool
    required: bool
    default: str | None


@dataclass(frozen=True, slots=True)
class ConnectionInputReference:
    name: str


@dataclass(frozen=True, slots=True)
class ConnectionInputTarget:
    kind: str
    name: str
    parts: tuple[str | ConnectionInputReference, ...]


@dataclass(frozen=True, slots=True)
class PluginConnectionInputs:
    inputs: tuple[ConnectionInput, ...] = ()
    targets: tuple[ConnectionInputTarget, ...] = ()


def parse_connection_inputs(
    value: object, *, server_ids: tuple[str, ...]
) -> dict[str, PluginConnectionInputs]:
    if (
        not isinstance(value, dict)
        or set(value) != {"servers"}
        or not isinstance(value["servers"], dict)
    ):
        raise ValueError("connection inputs require a servers object")
    if set(value["servers"]) - set(server_ids):
        raise ValueError("connection inputs reference an undeclared MCP server")
    result = {}
    for server_id, definition in value["servers"].items():
        if not isinstance(definition, dict) or set(definition) != {"inputs", "targets"}:
            raise ValueError("invalid connection input definition")
        if not isinstance(definition["inputs"], list) or not isinstance(
            definition["targets"], list
        ):
            raise ValueError("connection inputs and targets must be arrays")
        inputs = {}
        for item in definition["inputs"]:
            if not isinstance(item, dict) or set(item) != {
                "name",
                "title",
                "private",
                "required",
                "default",
            }:
                raise ValueError("invalid connection input fields")
            name = item["name"]
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                or name in inputs
            ):
                raise ValueError("connection input names must be unique identifiers")
            if not isinstance(item["title"], str) or not item["title"].strip():
                raise ValueError("connection input requires a title")
            if type(item["private"]) is not bool or type(item["required"]) is not bool:
                raise ValueError("connection input kinds must be explicit")
            if item["default"] is not None and (
                item["private"] or not isinstance(item["default"], str)
            ):
                raise ValueError("private inputs cannot contain defaults")
            inputs[name] = ConnectionInput(**item)
        targets, claimed, used = [], set(), set()
        for item in definition["targets"]:
            if not isinstance(item, dict) or set(item) != {"kind", "name", "parts"}:
                raise ValueError("invalid connection target fields")
            kind, name = item["kind"], item["name"]
            if kind not in {"endpoint", "header", "env", "oauth"} or not isinstance(
                name, str
            ):
                raise ValueError("connection inputs cannot modify executable fields")
            if kind == "endpoint" and name != "endpoint":
                raise ValueError("invalid endpoint input target")
            if kind == "oauth" and name not in {
                "client_id",
                "client_secret",
                "scope",
                "resource",
                "redirect_uri",
                "client_metadata_url",
            }:
                raise ValueError("invalid OAuth input target")
            if kind == "env" and (
                name in {"PLUGIN_ROOT", "PLUGIN_DATA", "PULSARA_API_KEY"}
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            ):
                raise ValueError("invalid or reserved environment input target")
            key = (kind, name.lower() if kind == "header" else name)
            if (
                not name
                or key in claimed
                or not isinstance(item["parts"], list)
                or not item["parts"]
            ):
                raise ValueError("connection input target is empty or duplicated")
            claimed.add(key)
            parts, private = [], False
            for part in item["parts"]:
                if not isinstance(part, dict) or len(part) != 1:
                    raise ValueError(
                        "connection template must contain literal/input parts"
                    )
                if set(part) == {"literal"} and isinstance(part["literal"], str):
                    parts.append(part["literal"])
                elif set(part) == {"input"} and part["input"] in inputs:
                    ref = inputs[part["input"]]
                    parts.append(ConnectionInputReference(ref.name))
                    used.add(ref.name)
                    private |= ref.private
                else:
                    raise ValueError("connection template references an unknown input")
            if (
                private
                and kind not in {"header", "env"}
                and not (kind == "oauth" and name == "client_secret")
            ):
                raise ValueError("private inputs cannot be placed in public fields")
            targets.append(ConnectionInputTarget(kind, name, tuple(parts)))
        if used != set(inputs):
            raise ValueError("connection input is not bound to any target")
        result[server_id] = PluginConnectionInputs(
            tuple(inputs[name] for name in sorted(inputs)), tuple(targets)
        )
    return result


def connection_input_defaults(
    definition: PluginConnectionInputs, *, owner, transport_kind: str
):
    """Project default public values and missing private references, without IO."""
    from pulsara_agent.mcp_config import NoAuth, _parse_auth
    from pulsara_agent.mcp_credentials import secret_to_dict
    from .mcp_connection import PluginMcpConnectionOverlay

    inputs = {item.name: item for item in definition.inputs}
    headers, environment, secret_env, secret_headers, oauth = {}, {}, {}, {}, {}
    endpoint = None
    for target in definition.targets:
        private = any(
            isinstance(part, ConnectionInputReference) and inputs[part.name].private
            for part in target.parts
        )
        parts = []
        for part in target.parts:
            if isinstance(part, str):
                parts.append(part)
            else:
                item = inputs[part.name]
                if item.private:
                    parts.append(
                        ManagedLocalCredentialReference(
                            McpCredentialBinding(owner, "input:" + item.name)
                        )
                    )
                elif item.default is not None:
                    parts.append(item.default)
                else:
                    raise ValueError(
                        "required public connection inputs must be supplied before package conversion"
                    )
        rendered = BoundSecretValue(tuple(parts)) if private else "".join(parts)
        if target.kind == "endpoint":
            if transport_kind == "stdio":
                raise ValueError("stdio input cannot define an HTTP endpoint")
            endpoint = rendered
        elif target.kind == "env":
            if transport_kind != "stdio":
                raise ValueError("HTTP input cannot define child environment")
            (secret_env if private else environment)[target.name] = rendered
        elif target.kind == "header":
            if transport_kind == "stdio":
                raise ValueError("stdio input cannot define HTTP headers")
            (secret_headers if private else headers)[target.name] = rendered
        else:
            oauth[target.name] = secret_to_dict(rendered) if private else rendered
    if oauth and secret_headers:
        raise ValueError(
            "OAuth and static secret headers cannot both own authorization"
        )
    auth = (
        _parse_auth({"type": "oauth", **oauth})
        if oauth
        else _parse_auth(
            {
                "type": "static_headers",
                "headers": {
                    name: secret_to_dict(value)
                    for name, value in secret_headers.items()
                },
            }
        )
        if secret_headers
        else NoAuth()
    )
    return PluginMcpConnectionOverlay(
        owner.server_id,
        transport_kind,
        endpoint,
        tuple(sorted(headers.items())),
        tuple(sorted(environment.items())),
        tuple(sorted(secret_env.items())),
        auth,
    )
