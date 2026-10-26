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
    INSTALL_LOOSE_SKILL = "INSTALL_LOOSE_SKILL"
    SET_LOOSE_SKILL_ENABLED = "SET_LOOSE_SKILL_ENABLED"
    REMOVE_LOOSE_SKILL = "REMOVE_LOOSE_SKILL"
    TRUST_HOOK_SOURCE = "TRUST_HOOK_SOURCE"
    REVOKE_HOOK_TRUST = "REVOKE_HOOK_TRUST"
    SET_HOOK_SOURCE_ENABLED = "SET_HOOK_SOURCE_ENABLED"
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
    CapabilityManagementAction.INSTALL_LOOSE_SKILL: (
        {"source_path"},
        {"name", "description"},
    ),
    CapabilityManagementAction.SET_LOOSE_SKILL_ENABLED: (
        {"skill_path", "enabled"},
        set(),
    ),
    CapabilityManagementAction.REMOVE_LOOSE_SKILL: ({"skill_path"}, set()),

    CapabilityManagementAction.TRUST_HOOK_SOURCE: ({"source_kind"}, {"plugin_id"}),
    CapabilityManagementAction.REVOKE_HOOK_TRUST: ({"source_kind"}, {"plugin_id"}),
    CapabilityManagementAction.SET_HOOK_SOURCE_ENABLED: (
        {"source_kind", "enabled"},
        {"plugin_id"},
    ),

    CapabilityManagementAction.ADD_LOCAL_MCP: ({"server_id"}, {"config"}),
    CapabilityManagementAction.UPDATE_LOCAL_MCP: (
        {"server_id"},
        {"config", "expected_identity"},
    ),
    CapabilityManagementAction.REMOVE_LOCAL_MCP: ({"server_id"}, {"expected_identity"}),
    CapabilityManagementAction.INSTALL_PLUGIN: (
        {"source_path"},
        {"replace", "source_format"},
    ),
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
        "scope": {
            "type": "string",
            "enum": [item.value for item in CapabilityManagementScope],
            "description": (
                'USER is the Host home; WORKSPACE is the GUI project directory. Copy an existing '
                "target's scope; choose the requested scope for installation."
            ),
        },
        "server_id": {
            "type": "string", "minLength": 1,
            "description": (
                "New ID for ADD_LOCAL_MCP; otherwise copy the installation target's independent or "
                'Plugin-local server_id, not runtime_server_id.'
            ),
        },
        "plugin_id": {
            "type": "string", "minLength": 1,
            "description": (
                'Plugin ID from the source target or install result. For OAuth actions, include only '
                'for a Plugin connection.'
            ),
        },
        "source_path": {
            "type": "string", "minLength": 1,
            "description": (
                'Absolute local source directory containing SKILL.md or the Plugin distribution root; '
                'download it first.'
            ),
        },
        "skill_path": {
            "type": "string", "minLength": 1,
            "description": (
                'Exact absolute installed SKILL.md path from list/inspect.'
            ),
        },
        "name": {
            "type": "string", "minLength": 1,
            "description": (
                'Override the installed loose Skill name; omission keeps its source name.'
            ),
        },
        "description": {
            "type": "string", "minLength": 1,
            "description": (
                'Override the installed loose Skill description; omission keeps its source description.'
            ),
        },
        "expected_identity": {
            "type": "string", "minLength": 1,
            "description": (
                'Optional observed independent-MCP guard; omit for automatic inspection. OAuth allows '
                'it only without plugin_id.'
            ),
        },
        "expected_package_install_id": {
            "type": "string", "minLength": 1,
            "description": (
                'Optional observed Plugin-install guard; omit for automatic inspection. OAuth allows it'
                ' only with plugin_id.'
            ),
        },
        "source_kind": {
            "type": "string", "enum": ["LOCAL", "PLUGIN"],
            "description": (
                'Hook source owner from its target: PLUGIN requires plugin_id; LOCAL omits it.'
            ),
        },
        "enabled": {
            "type": "boolean",
        },
        "replace": {
            "type": "boolean",
            "description": (
                'Omitted means false; true replaces the Plugin instance when requested by the user.'
            ),
        },
        "source_format": {
            "type": "string",
            "enum": ["native", "claude", "codex", "cursor"],
            "description": (
                'Plugin distribution format; omitted means native.'
            ),
        },
        "config": {
            "type": "object",
            "description": (
                'Omit to open the connection editor, prefilled for UPDATE. An object replaces the '
                'complete native entry. Public fields are documented in pulsara-mcp-installer; use the '
                'editor for credentials or unknown settings.'
            ),
        },
        "overlay": {
            "type": ["object", "null"],
            "description": (
                'Omit for the prefilled Plugin connection editor; null restores package/input defaults.'
                ' An object replaces the complete native overlay. It configures endpoint, public '
                'headers/environment and authentication; package-owned command, args, cwd and transport'
                ' stay fixed. Use the editor for credentials or unknown fields.'
            ),
        },
        "expected_overlay": {
            "type": ["object", "null"],
            "description": (
                'Optional observed prior overlay guard; omit for automatic inspection. null asserts '
                'that no override exists.'
            ),
        },
    }
    return {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    **{
                        name: properties[name]
                        for name in sorted(required | optional | {"scope"})
                    },
                    "action": {
                        "type": "string", "const": action.value,
                    },
                },
                "required": sorted(required | {"action", "scope"}),
                "additionalProperties": False,
            }
            for action, (required, optional) in _FIELDS.items()
        ],
    }


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
        "skill_path",
        "name",
        "description",
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
    for name in ("source_path", "skill_path"):
        if name in fields and not Path(fields[name]).is_absolute():
            raise ValueError("capability source paths must be absolute")
    if "source_kind" in fields and fields["source_kind"] not in {"LOCAL", "PLUGIN"}:
        raise ValueError("Hook source kind must be LOCAL or PLUGIN")
    if "HOOK" in action.value:
        kind = fields.get("source_kind")
        if "plugin_id" in fields and kind != "PLUGIN":
            raise ValueError("Plugin Hook identity requires source_kind PLUGIN")
        if (
            (kind == "PLUGIN") != ("plugin_id" in fields)
        ):
            raise ValueError("select the exact Plugin Hook identity")
    if fields.get("source_format", "native") not in {
        "native",
        "claude",
        "codex",
        "cursor",
    }:
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
