"""Closed model-facing management inputs; native owners retain execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from jsonschema import Draft202012Validator
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
    thaw_json,
)


class CapabilityManagementAction(StrEnum):
    INSTALL_LOOSE_SKILL = "INSTALL_LOOSE_SKILL"
    SET_LOOSE_SKILL_ENABLED = "SET_LOOSE_SKILL_ENABLED"
    REMOVE_LOOSE_SKILL = "REMOVE_LOOSE_SKILL"
    TRUST_HOOK_SOURCE = "TRUST_HOOK_SOURCE"
    REVOKE_HOOK_TRUST = "REVOKE_HOOK_TRUST"
    SET_HOOK_SOURCE_ENABLED = "SET_HOOK_SOURCE_ENABLED"
    ADD_LOCAL_MCP = "ADD_LOCAL_MCP"
    UPDATE_MCP_CONNECTION = "UPDATE_MCP_CONNECTION"
    REMOVE_LOCAL_MCP = "REMOVE_LOCAL_MCP"
    INSTALL_PLUGIN = "INSTALL_PLUGIN"
    SET_PLUGIN_ENABLED = "SET_PLUGIN_ENABLED"
    REMOVE_PLUGIN = "REMOVE_PLUGIN"
    AUTHORIZE_MCP = "AUTHORIZE_MCP"
    CLEAR_MCP_AUTHORIZATION = "CLEAR_MCP_AUTHORIZATION"


class CapabilityManagementScope(StrEnum):
    USER = "USER"
    WORKSPACE = "WORKSPACE"


@dataclass(frozen=True, slots=True)
class CapabilityManagementIntent:
    action: CapabilityManagementAction
    target: FrozenJsonObjectFact
    scope: CapabilityManagementScope | None
    fields: FrozenJsonObjectFact

    def to_dict(self) -> dict[str, object]:
        return {
            "target": thaw_json(self.target),
            "action": self.action.value,
            **({"scope": self.scope.value} if self.scope is not None else {}),
            **thaw_json(self.fields),
        }


def _object(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def mcp_public_config_schema(*, creating: bool):
    text = {"type": "string", "minLength": 1}
    mapping = {"type": "object", "additionalProperties": {"type": "string"}}
    shared = {
        "enabled": {"type": "boolean"},
        "auth": _object(
            {
                "type": {
                    "type": "string",
                    "enum": ["none", "oauth", "bearer", "static_headers"],
                }
            }
        ),
    }
    stdio = {
        "type": {"const": "stdio"},
        "command": text,
        "args": {"type": "array", "items": {"type": "string"}},
        "env": mapping,
        "cwd": text,
    }
    http = {"type": {"enum": ["streamable_http", "sse"]}, "endpoint": text}
    if creating:
        return {
            "oneOf": [
                _object(
                    {**shared, "transport": _object(stdio, ["type", "command"])},
                    ["transport"],
                ),
                _object(
                    {
                        **shared,
                        "transport": _object(http, ["type", "endpoint"]),
                        "public_headers": mapping,
                    },
                    ["transport"],
                ),
            ]
        }
    return _object(
        {
            **shared,
            "transport": {"anyOf": [_object(stdio), _object(http)]},
            "public_headers": mapping,
        }
    )


def capability_management_input_schema() -> dict[str, object]:
    from pulsara_agent.capability.source_query import capability_target_schema
    from pulsara_agent.tools.builtins.filesystem_contracts import path_target_schema

    scope = {"type": "string", "enum": ["USER", "WORKSPACE"]}
    boolean = {"type": "boolean"}
    branches = []
    for action in CapabilityManagementAction:
        install = action in {
            CapabilityManagementAction.INSTALL_PLUGIN,
            CapabilityManagementAction.INSTALL_LOOSE_SKILL,
        }
        kind = (
            "SKILL"
            if "SKILL" in action.value
            else "HOOK_SOURCE"
            if "HOOK" in action.value
            else "PLUGIN"
            if action
            in {
                CapabilityManagementAction.SET_PLUGIN_ENABLED,
                CapabilityManagementAction.REMOVE_PLUGIN,
            }
            else "MCP_CONNECTION"
        )
        props = {
            "target": path_target_schema()
            if install
            else capability_target_schema(kind),
            "action": {"const": action.value},
        }
        required = ["target", "action"]
        if install:
            props["scope"] = scope
            required.append("scope")
        if action.value.startswith("SET_"):
            props["enabled"] = boolean
            required.append("enabled")
        if action is CapabilityManagementAction.INSTALL_PLUGIN:
            props.update(
                replace=boolean,
                source_format={
                    "type": "string",
                    "enum": ["native", "claude", "codex", "cursor"],
                },
            )
        if action is CapabilityManagementAction.ADD_LOCAL_MCP:
            props["config"] = mcp_public_config_schema(creating=True)
        if action is CapabilityManagementAction.UPDATE_MCP_CONNECTION:
            props["config"] = {
                "anyOf": [mcp_public_config_schema(creating=False), {"type": "null"}]
            }
        branches.append(_object(props, required))
    return {"oneOf": branches}


def parse_capability_management_intent(
    value: Mapping[str, object], *, native_config: bool = False
) -> CapabilityManagementIntent:
    if not isinstance(value, Mapping):
        raise ValueError("capability parameters must be an object")
    schema = capability_management_input_schema()
    if native_config:
        # The user editor submits a complete owner-validated native entry, not
        # the model's restricted config selection. Root fields remain closed.
        for branch in schema["oneOf"]:
            if "config" in branch["properties"]:
                branch["properties"]["config"] = {"type": ["object", "null"]}
    if not Draft202012Validator(schema).is_valid(dict(value)):
        raise ValueError("Use only the selected action's fields and an exact Target.")
    target = value["target"]
    if target["kind"] != "PATH":
        from pulsara_agent.conversation_kernel.capability_targets import exact_target_path

        field = (
            "skill_path"
            if target["kind"] == "SKILL"
            else "plugin_path"
            if target["kind"] == "PLUGIN"
            else "config_path"
        )
        exact_target_path(target[field], field)
    return CapabilityManagementIntent(
        CapabilityManagementAction(value["action"]),
        freeze_json(value["target"]),
        CapabilityManagementScope(value["scope"]) if "scope" in value else None,
        freeze_json(
            {
                key: item
                for key, item in value.items()
                if key not in {"action", "target", "scope"}
            }
        ),
    )


__all__ = [
    "CapabilityManagementAction",
    "CapabilityManagementScope",
    "CapabilityManagementIntent",
    "capability_management_input_schema",
    "parse_capability_management_intent",
    "mcp_public_config_schema",
]
