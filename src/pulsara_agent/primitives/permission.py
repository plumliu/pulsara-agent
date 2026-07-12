"""Low-level, event-safe permission preset contracts.

The preset expansion table lives here so event schemas, Host run-boundary
facts, and the runtime permission gate validate the same durable value. This
module deliberately has no dependency on runtime policy classes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class PermissionMode(StrEnum):
    READ_ONLY = "read-only"
    ASK_PERMISSIONS = "ask-permissions"
    ACCEPT_EDITS = "accept-edits"
    BYPASS_PERMISSIONS = "bypass-permissions"


DEFAULT_PERMISSION_MODE = PermissionMode.BYPASS_PERMISSIONS


_PRESET_PERMISSION_PAYLOADS: dict[PermissionMode, dict[str, Any]] = {
    PermissionMode.READ_ONLY: {
        "profile": "read_only",
        "approval_policy": "on_request",
        "terminal_access": "off",
        "execution_boundary": "host",
        "network_isolated": False,
        "filesystem": {
            "read_file_scope": "host_local_text",
            "search_files_scope": "host_local_text_guarded_broad_roots",
            "write_file_scope": "workspace_only",
            "terminal": "off",
        },
    },
    PermissionMode.ASK_PERMISSIONS: {
        "profile": "trusted_host",
        "approval_policy": "on_request",
        "terminal_access": "ask",
        "execution_boundary": "host",
        "network_isolated": False,
        "filesystem": {
            "read_file_scope": "host_local_text",
            "search_files_scope": "host_local_text_guarded_broad_roots",
            "write_file_scope": "workspace_only",
            "terminal": "host_shell",
        },
    },
    PermissionMode.ACCEPT_EDITS: {
        "profile": "trusted_host",
        "approval_policy": "never",
        "terminal_access": "ask",
        "execution_boundary": "host",
        "network_isolated": False,
        "filesystem": {
            "read_file_scope": "host_local_text",
            "search_files_scope": "host_local_text_guarded_broad_roots",
            "write_file_scope": "workspace_only",
            "terminal": "host_shell",
        },
    },
    PermissionMode.BYPASS_PERMISSIONS: {
        "profile": "trusted_host",
        "approval_policy": "never",
        "terminal_access": "allow",
        "execution_boundary": "host",
        "network_isolated": False,
        "filesystem": {
            "read_file_scope": "host_local_text",
            "search_files_scope": "host_local_text_guarded_broad_roots",
            "write_file_scope": "workspace_only",
            "terminal": "host_shell",
        },
    },
}


def parse_permission_mode(value: str | PermissionMode) -> PermissionMode:
    if isinstance(value, PermissionMode):
        return value
    normalized = str(value).strip()
    try:
        return PermissionMode(normalized)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in PermissionMode)
        raise ValueError(
            f"Invalid permission mode: {value!r} (expected one of: {allowed})"
        ) from exc


def _copy_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _copy_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_json(item) for item in value]
    return value


def preset_permission_payload(mode: str | PermissionMode) -> dict[str, Any]:
    """Return an owned JSON copy of the canonical preset expansion."""

    return _copy_json(_PRESET_PERMISSION_PAYLOADS[parse_permission_mode(mode)])


class PresetPermissionPolicyFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: PermissionMode
    expanded_policy: dict[str, Any]

    @field_validator("expanded_policy", mode="before")
    @classmethod
    def _copy_expanded_policy(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            raise ValueError("expanded_policy must be a JSON object")
        return _copy_json(value)

    @model_validator(mode="after")
    def _validate_preset(self) -> "PresetPermissionPolicyFact":
        if self.expanded_policy != preset_permission_payload(self.mode):
            raise ValueError(
                f"expanded_policy must equal the {self.mode.value!r} preset"
            )
        return self


def preset_permission_policy_fact(
    mode: str | PermissionMode,
) -> PresetPermissionPolicyFact:
    parsed = parse_permission_mode(mode)
    return PresetPermissionPolicyFact(
        mode=parsed,
        expanded_policy=preset_permission_payload(parsed),
    )


__all__ = [
    "DEFAULT_PERMISSION_MODE",
    "PermissionMode",
    "PresetPermissionPolicyFact",
    "parse_permission_mode",
    "preset_permission_payload",
    "preset_permission_policy_fact",
]
