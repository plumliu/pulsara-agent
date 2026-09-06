import pytest

from pulsara_agent.capability.management_intent import (
    parse_capability_management_intent,
)


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "ADD_LOCAL_MCP", "server_id": "test"},
        {
            "action": "UPDATE_LOCAL_MCP",
            "server_id": "test",
            "config": {"enabled": False},
        },
        {
            "action": "REMOVE_LOCAL_MCP",
            "server_id": "test",
            "expected_identity": "exact-current",
        },
        {
            "action": "INSTALL_PLUGIN",
            "source_path": "/tmp/user-selected-plugin",
            "replace": False,
        },
        {
            "action": "SET_PLUGIN_ENABLED",
            "plugin_id": "example.plugin",
            "enabled": True,
        },
        {"action": "REMOVE_PLUGIN", "plugin_id": "example.plugin"},
        {
            "action": "CONFIGURE_PLUGIN_MCP_CONNECTION",
            "plugin_id": "example.plugin",
            "server_id": "test",
            "overlay": None,
        },
        {"action": "AUTHORIZE_MCP", "server_id": "test"},
        {
            "action": "CLEAR_MCP_AUTHORIZATION",
            "server_id": "test",
            "plugin_id": "example.plugin",
        },
    ],
)
@pytest.mark.parametrize("scope", ["USER", "WORKSPACE"])
def test_closed_public_actions_round_trip_without_reinterpreting_scope(payload, scope):
    value = {**payload, "scope": scope}
    assert parse_capability_management_intent(value).to_dict() == value


@pytest.mark.parametrize(
    "extra",
    [
        {"trusted": True},
        {"force": True},
        {"skip_confirmation": True},
        {"interactive": False},
        {"path": "/tmp/arbitrary-destination"},
        {"api_key": "private"},
        {"secret_changes": [{"value": "private"}]},
        {"raw_yaml": "servers: {}"},
        {"receipt": "invented"},
        {"capability_effects": {"workspace_write": True}},
        {"external_process_acceptance": "ACCEPTED"},
        {"command": "pulsara mcp add"},
    ],
)
def test_model_cannot_supply_private_values_or_authority_labels(extra):
    with pytest.raises(ValueError):
        parse_capability_management_intent(
            {
                "action": "ADD_LOCAL_MCP",
                "scope": "USER",
                "server_id": "test",
                **extra,
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "ADD_LOCAL_MCP", "server_id": "test", "config": '{"transport":{}}'},
        {"action": "REMOVE_LOCAL_MCP", "server_id": "test", "config": {}},
        {"action": "INSTALL_PLUGIN", "source_path": "relative/plugin"},
        {"action": "SET_PLUGIN_ENABLED", "plugin_id": "example", "enabled": 1},
        {
            "action": "AUTHORIZE_MCP",
            "server_id": "test",
            "expected_package_install_id": "package",
        },
        {
            "action": "AUTHORIZE_MCP",
            "server_id": "test",
            "plugin_id": "example",
            "expected_identity": "local",
        },
        {"action": "INSTALL_SKILL", "source_path": "/tmp/skill"},
    ],
)
def test_action_specific_shapes_reject_ambiguous_or_unsupported_inputs(payload):
    with pytest.raises(ValueError):
        parse_capability_management_intent({"scope": "USER", **payload})


def test_explicit_no_overlay_guard_remains_distinct_from_omitted_guard():
    value = {
        "action": "CONFIGURE_PLUGIN_MCP_CONNECTION",
        "scope": "USER",
        "plugin_id": "example",
        "server_id": "test",
    }
    omitted = parse_capability_management_intent(value)
    empty = parse_capability_management_intent({**value, "expected_overlay": None})
    assert omitted != empty
    assert "expected_overlay" not in omitted.to_dict()
    assert empty.to_dict()["expected_overlay"] is None
