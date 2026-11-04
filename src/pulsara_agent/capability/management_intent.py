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

from pulsara_agent.capability.source_query import capability_target_schema
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
    target: FrozenJsonObjectFact | None
    scope: CapabilityManagementScope | None
    fields: FrozenJsonObjectFact

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            **(
                {"target": thaw_json(self.target)}
                if self.target is not None
                else {"scope": self.scope.value}
            ),
            **thaw_json(self.fields),
        }


# Each row is one exact public action/target branch. Missing and null overlay
# guards remain distinct; candidates are validated by their native owners.
_BRANCHES = [
    (
        "INSTALL_LOOSE_SKILL",
        None,
        None,
        ["scope", "source_path"],
        ["name", "description"],
    ),
    ("SET_LOOSE_SKILL_ENABLED", "SKILL", None, ["enabled"], []),
    ("REMOVE_LOOSE_SKILL", "SKILL", None, [], []),
    *[
        (
            a,
            "HOOK_SOURCE",
            source,
            ["enabled"] if a == "SET_HOOK_SOURCE_ENABLED" else [],
            [],
        )
        for a in ("TRUST_HOOK_SOURCE", "REVOKE_HOOK_TRUST", "SET_HOOK_SOURCE_ENABLED")
        for source in ("LOCAL", "PLUGIN")
    ],
    ("ADD_LOCAL_MCP", "MCP_CONNECTION", "LOCAL", [], ["config"]),
    (
        "UPDATE_LOCAL_MCP",
        "MCP_CONNECTION",
        "LOCAL",
        [],
        ["config", "expected_identity"],
    ),
    ("REMOVE_LOCAL_MCP", "MCP_CONNECTION", "LOCAL", [], ["expected_identity"]),
    (
        "INSTALL_PLUGIN",
        None,
        None,
        ["scope", "source_path"],
        ["replace", "source_format"],
    ),
    (
        "SET_PLUGIN_ENABLED",
        "PLUGIN",
        None,
        ["enabled"],
        ["expected_package_install_id"],
    ),
    ("REMOVE_PLUGIN", "PLUGIN", None, [], ["expected_package_install_id"]),
    (
        "CONFIGURE_PLUGIN_MCP_CONNECTION",
        "MCP_CONNECTION",
        "PLUGIN",
        [],
        ["overlay", "expected_overlay", "expected_package_install_id"],
    ),
    *[
        (
            a,
            "MCP_CONNECTION",
            source,
            [],
            ["expected_identity"]
            if source == "LOCAL"
            else ["expected_package_install_id"],
        )
        for a in ("AUTHORIZE_MCP", "CLEAR_MCP_AUTHORIZATION")
        for source in ("LOCAL", "PLUGIN")
    ],
]


def capability_management_input_schema() -> dict[str, object]:
    """Closed action union; nested candidates use the sole native parser."""
    properties = {
        "scope": {
            "type": "string",
            "enum": ["USER", "WORKSPACE"],
            "description": "Install in USER (Host home) or WORKSPACE (GUI project).",
        },
        "source_path": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Absolute local directory containing SKILL.md or the Plugin distribution "
                "root; download first."
            ),
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "Loose Skill name override; omission keeps its source name.",
        },
        "description": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Loose Skill description override; omission keeps its source description."
            ),
        },
        "enabled": {"type": "boolean"},
        "config": {
            "type": "object",
            "description": (
                "Omit for editor (UPDATE prefilled). Object replaces the complete native "
                "entry. Public fields: pulsara-mcp-installer; credentials/unknown settings: "
                "editor."
            ),
        },
        "expected_identity": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Observed local MCP config guard; omit for owner inspection."
            ),
        },
        "replace": {
            "type": "boolean",
            "description": "Defaults false; replace only when user-requested.",
        },
        "source_format": {
            "type": "string",
            "enum": ["native", "claude", "codex", "cursor"],
            "description": "Plugin format; defaults to native.",
        },
        "expected_package_install_id": {
            "type": "string",
            "minLength": 1,
            "description": "Observed Plugin install guard; omit for owner inspection.",
        },
        "overlay": {
            "type": ["object", "null"],
            "description": (
                "Omit for prefilled editor; null restores package/input defaults; object "
                "replaces the complete native overlay. Endpoint, public headers/env and auth "
                "only; package command/args/cwd/transport stay fixed. Credentials/unknown "
                "fields: editor."
            ),
        },
        "expected_overlay": {
            "type": ["object", "null"],
            "description": (
                "Observed overlay guard; omit for owner inspection; null asserts no override."
            ),
        },
    }
    branches = []
    for action, kind, source, required, optional in _BRANCHES:
        props = {"action": {"type": "string", "const": action}}
        if kind is not None:
            props["target"] = capability_target_schema(kind, source_kind=source)
        props.update({name: properties[name] for name in required + optional})
        branches.append(
            {
                "type": "object",
                "properties": props,
                "required": ["action"] + (["target"] if kind else []) + required,
                "additionalProperties": False,
            }
        )
    return {"type": "object", "oneOf": branches}


def parse_capability_management_intent(
    value: Mapping[str, object],
) -> CapabilityManagementIntent:
    if not isinstance(value, Mapping):
        raise ValueError("capability intent must be an object")
    if not Draft202012Validator(capability_management_input_schema()).is_valid(
        dict(value)
    ):
        raise ValueError("capability action fields are incomplete or unsupported")
    action = CapabilityManagementAction(value["action"])
    target = dict(value["target"]) if "target" in value else None
    scope = CapabilityManagementScope(value["scope"]) if "scope" in value else None
    fields = {
        key: item
        for key, item in value.items()
        if key not in {"action", "scope", "target"}
    }
    identities = {**fields, **(target or {})}
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
        if name in identities and (
            not isinstance(identities[name], str) or not identities[name].strip()
        ):
            raise ValueError("capability identity must be non-empty text")
    for name in ("enabled", "replace"):
        if name in fields and type(fields[name]) is not bool:
            raise ValueError("capability enable/replace selection must be boolean")
    for name in ("source_path", "skill_path"):
        if name in identities and not Path(identities[name]).is_absolute():
            raise ValueError("capability source paths must be absolute")
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
    return CapabilityManagementIntent(
        action,
        freeze_json(target) if target is not None else None,
        scope,
        freeze_json(fields),
    )
