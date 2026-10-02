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
        "Install an independent Skill; existing destinations are not overwritten."
    ),
    CapabilityManagementAction.SET_LOOSE_SKILL_ENABLED: (
        "Toggle an inspected, eligible independent Skill copy; Plugin and bundled Skills are excluded."
    ),
    CapabilityManagementAction.REMOVE_LOOSE_SKILL: (
        "Remove an independent Skill directory after inspecting removal eligibility; "
        "discovery alone does not imply removability. Plugin and bundled Skills are excluded."
    ),
    CapabilityManagementAction.TRUST_HOOK_SOURCE: (
        "Request user review and trust of the full current Hook source; does not enable it or its Plugin."
    ),
    CapabilityManagementAction.REVOKE_HOOK_TRUST: (
        "Revoke Hook source trust while keeping its definitions."
    ),
    CapabilityManagementAction.SET_HOOK_SOURCE_ENABLED: (
        "Toggle a Hook source; does not grant trust, enable its Plugin or edit definitions."
    ),
    CapabilityManagementAction.ADD_LOCAL_MCP: (
        "Add an independent MCP connection with a new server_id; not a Plugin declaration."
    ),
    CapabilityManagementAction.UPDATE_LOCAL_MCP: (
        "Edit an existing independent MCP connection, including its enabled setting."
    ),
    CapabilityManagementAction.REMOVE_LOCAL_MCP: (
        "Remove an independent MCP entry and dedicated local credentials/grants; "
        "keeps other entries and the server program. Plugin declarations are excluded."
    ),
    CapabilityManagementAction.INSTALL_PLUGIN: (
        "Install or replace a whole Plugin as disabled; enable separately with SET_PLUGIN_ENABLED."
    ),
    CapabilityManagementAction.SET_PLUGIN_ENABLED: (
        "Toggle the whole Plugin; enabling requires user component/credential-destination review. "
        "Hook trust remains separate."
    ),
    CapabilityManagementAction.REMOVE_PLUGIN: (
        "Remove the whole Plugin and dedicated credentials; preserve Plugin data and the original source."
    ),
    CapabilityManagementAction.CONFIGURE_PLUGIN_MCP_CONNECTION: (
        "Configure an existing Plugin MCP connection; execution fields remain package-owned."
    ),
    CapabilityManagementAction.AUTHORIZE_MCP: (
        "Start interactive authorization for an already configured OAuth connection; "
        "does not configure authentication or enable the source."
    ),
    CapabilityManagementAction.CLEAR_MCP_AUTHORIZATION: (
        "Clear local OAuth grants; keep the connection configuration and remote tokens."
    ),
}


def capability_management_input_schema() -> dict[str, object]:
    """Closed action union; nested candidates use the sole native parser."""
    properties = {
        "scope": {
            "type": "string",
            "enum": [item.value for item in CapabilityManagementScope],
            "description": (
                "USER: Host's home; WORKSPACE: GUI project directory, never terminal cwd. "
                "Copy existing target scope; use the user's selected scope for installation."
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
                "Exact Plugin ID from source target, install result or user, never paths/runtime IDs. "
                "For OAuth actions, include for a Plugin connection; omit for an independent MCP."
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
            "description": "Independent MCP guard; normally omit, never guess. OAuth actions allow it only without plugin_id.",
        },
        "expected_package_install_id": {
            "type": "string", "minLength": 1,
            "description": "Plugin guard; normally omit, never infer from manifest version. OAuth actions allow it only with plugin_id.",
        },
        "source_kind": {
            "type": "string", "enum": ["LOCAL", "PLUGIN"],
            "description": "Hook owner: PLUGIN requires plugin_id; LOCAL forbids it. Copy from the source target.",
        },
        "enabled": {
            "type": "boolean",
            "description": "Whether to enable the selected Skill, Plugin or Hook source.",
        },
        "replace": {
            "type": "boolean",
            "description": "Plugin only: default false; true for user-requested replacement of that instance.",
        },
        "source_format": {
            "type": "string",
            "enum": ["native", "claude", "codex", "cursor"],
            "description": "Plugin distribution format for the official importer; default native.",
        },
        "config": {
            "type": "object",
            "description": (
                "Omit for the connection editor (UPDATE is prefilled). An object replaces the "
                "complete native entry, not a patch; preserve existing settings. Public shapes "
                "are in the MCP installer reference. Use the editor for credentials or unknown "
                "settings; do not read private settings or implementation to construct config."
            ),
        },
        "overlay": {
            "type": ["object", "null"],
            "description": (
                "Omit for the prefilled Plugin connection editor; null clears instance overrides, "
                "restoring package/input defaults. An object replaces the complete native overlay, "
                "not a patch or local MCP config. Only endpoint, public headers/environment and "
                "authentication are configurable; command, args, cwd and transport kind stay "
                "package-owned. Use the editor for unknown fields or credentials; the inspect "
                "summary is not an overlay template."
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
                        # Root-union lowering merges both required and allowed fields.
                        # Keep the exact per-action signature beside its wire enum.
                        "description": (
                            f"Required: {', '.join(sorted(required)) or 'none'}. "
                            f"Optional: {', '.join(sorted(optional)) or 'none'}. "
                            + _ACTION_DESCRIPTIONS[action]
                        ),
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
