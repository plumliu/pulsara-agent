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


# Keep operation guidance on the actual action property sent to the provider;
# callers need not load an installer Skill to choose the correct owner.
_ACTION_DESCRIPTIONS = {
    CapabilityManagementAction.INSTALL_LOOSE_SKILL: (
        "Install one loose Skill from a local directory into the selected scope. "
        "Does not overwrite or install a Plugin. For user-requested replacement, "
        "prepare the replacement source first; inspect/remove the eligible exact old copy, then install."
    ),
    CapabilityManagementAction.SET_LOOSE_SKILL_ENABLED: (
        "Enable or disable one observed loose Skill copy by its exact SKILL.md path; "
        "inspect its management eligibility first. Does not control Plugin or bundled Skills."
    ),
    CapabilityManagementAction.REMOVE_LOOSE_SKILL: (
        "Remove one authorized loose Skill directory by its exact SKILL.md path. "
        "Inspect removal eligibility first: discovery alone does not imply removability. "
        "Does not remove a Plugin component or a bundled Skill."
    ),
    CapabilityManagementAction.TRUST_HOOK_SOURCE: (
        "Request user review and trust of the full current Hook definitions for one LOCAL "
        "or PLUGIN source. Does not enable the source or its Plugin; the model cannot accept review."
    ),
    CapabilityManagementAction.REVOKE_HOOK_TRUST: (
        "Revoke trust for one LOCAL or PLUGIN Hook source without deleting its definitions."
    ),
    CapabilityManagementAction.SET_HOOK_SOURCE_ENABLED: (
        "Enable or disable one LOCAL or PLUGIN Hook source. Does not grant trust or enable "
        "its Plugin. Hook actions do not add, edit or delete definitions."
    ),
    CapabilityManagementAction.ADD_LOCAL_MCP: (
        "Add an independent MCP connection with a new server_id. Omit config to open the "
        "connection editor. Does not add a server to a Plugin."
    ),
    CapabilityManagementAction.UPDATE_LOCAL_MCP: (
        "Edit an existing independent MCP connection. Omit config for the prefilled editor; "
        "a supplied config replaces the entire entry, not selected fields. Enable/disable "
        "this connection through that editor or a complete config's enabled field."
    ),
    CapabilityManagementAction.REMOVE_LOCAL_MCP: (
        "Remove one authorized independent MCP entry and its dedicated local credentials "
        "and authorization. Keeps the configuration file and other entries; does not "
        "delete a Plugin's MCP declaration or uninstall the server program."
    ),
    CapabilityManagementAction.INSTALL_PLUGIN: (
        "Install a whole local Plugin distribution; use source_format for official import. "
        "replace:true replaces an existing instance only when the user requested replacement. "
        "Both installation and replacement publish a disabled instance; enable separately."
    ),
    CapabilityManagementAction.SET_PLUGIN_ENABLED: (
        "Enable or disable the whole exact Plugin instance. Enabling always requires user "
        "review of current components and credential destinations. Does not grant Hook trust."
    ),
    CapabilityManagementAction.REMOVE_PLUGIN: (
        "Remove the authorized exact Plugin instance and its dedicated credentials as a whole. "
        "Preserves Plugin data and the original source; existing consumers may finish using "
        "the old package. Does not delete individual component declarations."
    ),
    CapabilityManagementAction.CONFIGURE_PLUGIN_MCP_CONNECTION: (
        "Configure one existing Plugin MCP connection using plugin_id plus its local server_id. "
        "Omit overlay for the prefilled editor. overlay:null clears instance overrides, not "
        "the component, restoring package/input defaults. Cannot change package command, "
        "args, cwd or transport kind."
    ),
    CapabilityManagementAction.AUTHORIZE_MCP: (
        "Start user-interactive OAuth for an already configured OAuth connection. Include "
        "plugin_id for a Plugin component; omit it for an independent MCP. Does not configure "
        "authentication, enable the source or grant Hook trust."
    ),
    CapabilityManagementAction.CLEAR_MCP_AUTHORIZATION: (
        "Clear the connection's local OAuth grants without removing its configuration or "
        "revoking remote tokens. Include plugin_id only for a Plugin component."
    ),
}


def capability_management_input_schema() -> dict[str, object]:
    """Closed action union; nested candidates use the sole native parser."""
    properties = {
        "action": {
            "type": "string",
            "enum": [item.value for item in CapabilityManagementAction],
        },
        "scope": {
            "type": "string",
            "enum": [item.value for item in CapabilityManagementScope],
            "description": (
                "USER: Host's home; WORKSPACE: GUI directory. Copy existing target scope; "
                "terminal cwd never selects it."
            ),
        },
        "server_id": {
            "type": "string", "minLength": 1,
            "description": (
                "New ID for ADD; otherwise exact independent configuration or Plugin-local ID "
                "from the installation target. Not runtime_server_id, tool name or ref."
            ),
        },
        "plugin_id": {
            "type": "string", "minLength": 1,
            "description": (
                "Exact Plugin ID from source target, install result or user; never derive from paths/runtime IDs."
            ),
        },
        "source_path": {
            "type": "string", "minLength": 1,
            "description": (
                "Absolute local source directory: Skill containing SKILL.md or Plugin distribution "
                "root. Download separately; not an installation destination."
            ),
        },
        "skill_path": {
            "type": "string", "minLength": 1,
            "description": "Exact absolute installed SKILL.md path copied from list/inspect, not its directory or author source.",
        },
        "name": {
            "type": "string", "minLength": 1,
            "description": "Optional loose Skill metadata normalization; omit to retain the source's parsed name.",
        },
        "description": {
            "type": "string", "minLength": 1,
            "description": "Optional loose Skill metadata normalization; omit to retain its source description.",
        },
        "expected_identity": {
            "type": "string", "minLength": 1,
            "description": "Independent MCP change guard. Normally omit for fresh inspection; never invent.",
        },
        "expected_package_install_id": {
            "type": "string", "minLength": 1,
            "description": "Plugin change guard. Normally omit; never infer from manifest version.",
        },
        "source_kind": {
            "type": "string", "enum": ["LOCAL", "PLUGIN"],
            "description": "Hook owner only: LOCAL uses scope; PLUGIN also requires plugin_id. Copy the source target.",
        },
        "enabled": {
            "type": "boolean",
            "description": "Selected Skill/Plugin/Hook enablement; not trust, OAuth authorization or proof of adoption.",
        },
        "replace": {
            "type": "boolean",
            "description": "Plugin only: default false; true for user-requested replacement of that instance.",
        },
        "source_format": {
            "type": "string",
            "enum": ["native", "claude", "codex", "cursor"],
            "description": "Plugin distribution format; default native. Official import installs supported components and reports unsupported host features; do not rewrite manifests or claim full host compatibility.",
        },
        "config": {
            "type": "object",
            "description": (
                "Prefer omission for the connection editor (UPDATE is prefilled). A supplied "
                "object replaces the complete native entry, not a patch. HTTP example: "
                '{"display_name":"Docs","enabled":true,"transport":{"type":"streamable_http",'
                '"endpoint":"https://example.org/mcp"},"auth":{"type":"none"}}. '
                "Explicitly approved loopback HTTP requires allow_http_localhost:true inside transport. "
                'stdio transport uses type:"stdio", command, args (array), cwd (workspace-relative), env (public only). '
                "Preserve existing settings. Use the editor for credentials or unknown configuration; "
                "do not read private settings/repository implementation to build this object."
            ),
        },
        "overlay": {
            "type": ["object", "null"],
            "description": (
                "Prefer omission to open the Plugin connection editor. null clears overrides. "
                "A supplied object must be the complete native overlay, not a patch or local "
                "MCP config; do not guess its fields or copy the inspect summary as a template. "
                "Only endpoint, public headers/environment and authentication can be configured; "
                "executable fields and transport kind belong to the package. Secrets stay in the editor."
            ),
        },
        "expected_overlay": {
            "type": ["object", "null"],
            "description": "Prior overlay guard; normally omit. null asserts no existing override, not a request to clear it.",
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
                        "description": _ACTION_DESCRIPTIONS[action],
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
