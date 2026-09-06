"""Closed public intent for the single model-facing capability management tool.

Nested MCP and Plugin connection candidates are validated by their existing
native owners during preparation. This boundary never accepts secret mutations,
an execution permit, an arbitrary destination path, or a serialized config blob.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
    thaw_json,
)


class CapabilityManagementAction(StrEnum):
    ADD_LOCAL_MCP = "ADD_LOCAL_MCP"
    UPDATE_LOCAL_MCP = "UPDATE_LOCAL_MCP"
    REMOVE_LOCAL_MCP = "REMOVE_LOCAL_MCP"
    INSTALL_PLUGIN = "INSTALL_PLUGIN"
    SET_PLUGIN_ENABLED = "SET_PLUGIN_ENABLED"
    REMOVE_PLUGIN = "REMOVE_PLUGIN"
    CONFIGURE_PLUGIN_MCP_CONNECTION = "CONFIGURE_PLUGIN_MCP_CONNECTION"
    AUTHORIZE_MCP = "AUTHORIZE_MCP"
    CLEAR_MCP_AUTHORIZATION = "CLEAR_MCP_AUTHORIZATION"


class CapabilityManagementScope(StrEnum):
    USER = "USER"
    WORKSPACE = "WORKSPACE"


@dataclass(frozen=True, slots=True)
class CapabilityManagementIntent:
    action: CapabilityManagementAction
    scope: CapabilityManagementScope
    fields: FrozenJsonObjectFact

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "scope": self.scope.value,
            **thaw_json(self.fields),
        }


# Presence and null are deliberately distinct for expected_overlay: an omitted
# guard is resolved by inspection, whereas explicit null asserts no overlay.
_FIELDS = {
    CapabilityManagementAction.ADD_LOCAL_MCP: ({"server_id"}, {"config"}),
    CapabilityManagementAction.UPDATE_LOCAL_MCP: (
        {"server_id"},
        {"config", "expected_identity"},
    ),
    CapabilityManagementAction.REMOVE_LOCAL_MCP: ({"server_id"}, {"expected_identity"}),
    CapabilityManagementAction.INSTALL_PLUGIN: ({"source_path"}, {"replace", "source_format"}),
    CapabilityManagementAction.SET_PLUGIN_ENABLED: (
        {"plugin_id", "enabled"},
        {"expected_package_install_id"},
    ),
    CapabilityManagementAction.REMOVE_PLUGIN: (
        {"plugin_id"},
        {"expected_package_install_id"},
    ),
    CapabilityManagementAction.CONFIGURE_PLUGIN_MCP_CONNECTION: (
        {"plugin_id", "server_id"},
        {"overlay", "expected_overlay", "expected_package_install_id"},
    ),
    CapabilityManagementAction.AUTHORIZE_MCP: (
        {"server_id"},
        {"plugin_id", "expected_identity", "expected_package_install_id"},
    ),
    CapabilityManagementAction.CLEAR_MCP_AUTHORIZATION: (
        {"server_id"},
        {"plugin_id", "expected_identity", "expected_package_install_id"},
    ),
}


def capability_management_input_schema() -> dict[str, object]:
    """Closed action union; nested candidates use the sole native parser."""
    properties = {
        "action": {"type": "string", "enum": [item.value for item in CapabilityManagementAction]},
        "scope": {"type": "string", "enum": [item.value for item in CapabilityManagementScope]},
        **{name: {"type": "string", "minLength": 1} for name in (
            "server_id", "plugin_id", "source_path", "expected_identity", "expected_package_install_id")},
        "enabled": {"type": "boolean"}, "replace": {"type": "boolean"},
        "source_format": {"type": "string", "enum": ["native", "claude", "codex", "cursor"]},
        "config": {"type": "object", "description": (
            'Complete native public MCP entry, not a patch. HTTP example: '
            '{"display_name":"Docs","enabled":true,"transport":{"type":"streamable_http",'
            '"endpoint":"https://example.org/mcp"},"auth":{"type":"none"}}. '
            'For an explicitly approved loopback HTTP endpoint put allow_http_localhost:true inside transport. '
            'stdio transport uses type:"stdio", command, args (array), cwd (workspace-relative), env (public only). '
            'UPDATE replaces the whole entry: preserve existing settings; if you do not have the complete '
            'configuration, omit config to open the user editor prefilled from current truth. '
            'Never read private settings or repository source to construct this object. '
            'For credentials omit config and let the user select authentication and enter secrets in the editor.'
        )},
        "overlay": {"type": ["object", "null"], "description": "Finite Plugin MCP connection overlay; null clears it. Executable fields cannot be overridden."},
        "expected_overlay": {"type": ["object", "null"]},
    }
    return {"type": "object", "oneOf": [
        {"type": "object", "properties": {
            **{name: properties[name] for name in sorted(required | optional | {"scope"})},
            "action": {"type": "string", "const": action.value},
        }, "required": sorted(required | {"action", "scope"}), "additionalProperties": False}
        for action, (required, optional) in _FIELDS.items()
    ]}


def parse_capability_management_intent(
    value: Mapping[str, object],
) -> CapabilityManagementIntent:
    if not isinstance(value, Mapping):
        raise ValueError("capability intent must be an object")
    try:
        action = CapabilityManagementAction(value["action"])
        scope = CapabilityManagementScope(value["scope"])
    except (KeyError, ValueError, TypeError):
        raise ValueError("capability action and scope must be selected") from None
    required, optional = _FIELDS[action]
    fields = {
        key: item for key, item in value.items() if key not in {"action", "scope"}
    }
    if not required <= set(fields) or set(fields) - (required | optional):
        raise ValueError("capability action fields are incomplete or unsupported")
    for name in (
        "server_id",
        "plugin_id",
        "source_path",
        "expected_identity",
        "expected_package_install_id",
    ):
        if name in fields and (
            not isinstance(fields[name], str) or not fields[name].strip()
        ):
            raise ValueError("capability identity must be non-empty text")
    for name in ("enabled", "replace"):
        if name in fields and type(fields[name]) is not bool:
            raise ValueError("capability enable/replace selection must be boolean")
    if "source_path" in fields and not Path(fields["source_path"]).is_absolute():
        raise ValueError("Plugin source must be an existing absolute local source path")
    if fields.get("source_format", "native") not in {"native", "claude", "codex", "cursor"}:
        raise ValueError("Plugin source format must be explicitly selected")
    for name in ("config", "overlay", "expected_overlay"):
        if name not in fields:
            continue
        if fields[name] is None and name != "config":
            continue
        if not isinstance(fields[name], Mapping):
            raise ValueError(
                "capability configuration must be a typed public object, not serialized text"
            )
    if action in {
        CapabilityManagementAction.AUTHORIZE_MCP,
        CapabilityManagementAction.CLEAR_MCP_AUTHORIZATION,
    }:
        if "plugin_id" in fields:
            if "expected_identity" in fields:
                raise ValueError("Plugin authorization cannot use a local MCP guard")
        elif "expected_package_install_id" in fields:
            raise ValueError("local MCP authorization cannot use a Plugin guard")
    return CapabilityManagementIntent(action, scope, freeze_json(fields))
