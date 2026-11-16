"""The fourteen closed management actions expose only their minimal Targets."""

import pytest
from pulsara_agent.capability.management_intent import (
    CapabilityManagementAction,
    parse_capability_management_intent,
)

TARGETS = {
    "SKILL": {"kind": "SKILL", "skill_path": "/exact/review/SKILL.md"},
    "HOOK_SOURCE": {"kind": "HOOK_SOURCE", "config_path": "/exact/hooks.json"},
    "MCP_CONNECTION": {
        "kind": "MCP_CONNECTION",
        "config_path": "/exact/mcp.yaml",
        "server_id": "docs",
    },
    "PLUGIN": {"kind": "PLUGIN", "plugin_path": "/exact/example"},
    "PATH": {"kind": "PATH", "path": "downloads/source"},
}


def arguments(action):
    if action.startswith("INSTALL_"):
        return {"target": TARGETS["PATH"], "action": action, "scope": "WORKSPACE"}
    kind = (
        "SKILL"
        if "SKILL" in action
        else "HOOK_SOURCE"
        if "HOOK" in action
        else "PLUGIN"
        if "PLUGIN" in action
        else "MCP_CONNECTION"
    )
    return {
        "target": TARGETS[kind],
        "action": action,
        **({"enabled": True} if action.startswith("SET_") else {}),
    }


@pytest.mark.parametrize("action", list(CapabilityManagementAction))
def test_all_actions_round_trip_the_current_exact_fields(action):
    value = arguments(action.value)
    assert parse_capability_management_intent(value).to_dict() == value


@pytest.mark.parametrize(
    "extra",
    [
        {"trusted": True},
        {"force": True},
        {"skip_confirmation": True},
        {"interactive": False},
        {"path": "/tmp/destination"},
        {"api_key": "private"},
        {"secret_changes": [{"value": "private"}]},
        {"raw_yaml": "servers: {}"},
        {"receipt": "invented"},
        {"capability_effects": {"workspace_write": True}},
        {"external_process_acceptance": "ACCEPTED"},
        {"command": "pulsara mcp add"},
        {"scope": "USER"},
        {"server_id": "docs"},
        {"expected_identity": "observed"},
    ],
)
def test_model_cannot_supply_private_values_authority_or_flat_identity(extra):
    with pytest.raises(ValueError):
        parse_capability_management_intent({**arguments("ADD_LOCAL_MCP"), **extra})


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"enabled": False},
        {"transport": {"env": {}}},
        {"public_headers": {}},
        {"auth": {}},
    ],
)
def test_patch_omission_null_and_explicit_empty_values_stay_distinct(config):
    value = {**arguments("UPDATE_MCP_CONNECTION"), "config": config}
    assert parse_capability_management_intent(value).to_dict() == value
    assert parse_capability_management_intent(
        value
    ) != parse_capability_management_intent(arguments("UPDATE_MCP_CONNECTION"))


@pytest.mark.parametrize(
    "config",
    [
        {"scope_policy": "ROOT_ONLY"},
        {"display_name": "override"},
        {"effect_policy": {}},
        {"auth": {"type": "bearer", "reference": {}}},
        {"transport": {"type": "stdio", "endpoint": "https://example.com"}},
    ],
)
def test_public_config_does_not_expose_user_advanced_or_secret_inputs(config):
    with pytest.raises(ValueError):
        parse_capability_management_intent(
            {**arguments("UPDATE_MCP_CONNECTION"), "config": config}
        )


def test_user_native_config_is_private_and_root_fields_remain_closed():
    candidate = {
        **arguments("UPDATE_MCP_CONNECTION"),
        "config": {"scope_policy": "ROOT_ONLY", "default_tool_timeout_ms": 3000},
    }
    assert (
        parse_capability_management_intent(candidate, native_config=True).to_dict()
        == candidate
    )
    with pytest.raises(ValueError):
        parse_capability_management_intent(
            {**candidate, "expected_identity": "invented"}, native_config=True
        )


@pytest.mark.parametrize("path", ["relative/SKILL.md", " ", None])
def test_capability_targets_require_absolute_paths(path):
    with pytest.raises(ValueError):
        parse_capability_management_intent(
            {
                "action": "REMOVE_LOOSE_SKILL",
                "target": {"kind": "SKILL", "skill_path": path},
            }
        )


def test_install_path_and_scope_do_not_allow_destination_or_metadata_overrides():
    value = arguments("INSTALL_LOOSE_SKILL")
    for extra in (
        {"name": "override"},
        {"description": "override"},
        {"replace": True},
        {"scope": None},
    ):
        with pytest.raises(ValueError):
            parse_capability_management_intent({**value, **extra})
    with pytest.raises(ValueError):
        parse_capability_management_intent(
            {**arguments("ADD_LOCAL_MCP"), "config": None}
        )
