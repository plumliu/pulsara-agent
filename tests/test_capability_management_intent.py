import pytest

from pulsara_agent.capability.management_intent import (
    parse_capability_management_intent,
)


@pytest.mark.parametrize("scope", ["USER", "WORKSPACE"])
@pytest.mark.parametrize("source", ["LOCAL", "PLUGIN"])
def test_authorization_target_keeps_source_specific_guards(scope, source):
    target = {
        "kind": "MCP_CONNECTION",
        "scope": scope,
        "source_kind": source,
        "server_id": "docs",
    }
    guard = "expected_identity"
    if source == "PLUGIN":
        target["plugin_id"] = "package"
        guard = "expected_package_install_id"
    value = {"action": "AUTHORIZE_MCP", "target": target, guard: "observed"}
    assert parse_capability_management_intent(value).to_dict() == value
    wrong = "expected_identity" if source == "PLUGIN" else "expected_package_install_id"
    with pytest.raises(ValueError):
        parse_capability_management_intent({**value, wrong: "foreign"})


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
        {"scope": "USER"},
        {"server_id": "docs"},
    ],
)
def test_model_cannot_supply_private_values_authority_or_flat_identity(extra):
    with pytest.raises(ValueError):
        parse_capability_management_intent(
            {
                "action": "ADD_LOCAL_MCP",
                "target": {
                    "kind": "MCP_CONNECTION",
                    "scope": "USER",
                    "source_kind": "LOCAL",
                    "server_id": "docs",
                },
                **extra,
            }
        )


def test_overlay_absence_null_and_object_survive_public_round_trip():
    value = {
        "action": "CONFIGURE_PLUGIN_MCP_CONNECTION",
        "target": {
            "kind": "MCP_CONNECTION",
            "scope": "USER",
            "source_kind": "PLUGIN",
            "plugin_id": "example",
            "server_id": "docs",
        },
    }
    omitted = parse_capability_management_intent(value)
    assert (
        "overlay" not in omitted.to_dict()
        and "expected_overlay" not in omitted.to_dict()
    )
    for overlay in (None, {"server_id": "docs"}):
        candidate = {**value, "overlay": overlay, "expected_overlay": overlay}
        assert parse_capability_management_intent(candidate).to_dict() == candidate
        assert parse_capability_management_intent(candidate) != omitted


def test_identity_semantics_are_checked_without_changing_values():
    value = {
        "action": "REMOVE_LOOSE_SKILL",
        "target": {"kind": "SKILL", "skill_path": "/exact/root/SKILL.md"},
    }
    intent = parse_capability_management_intent(value)
    assert intent.scope is None and intent.to_dict() == value
    for path in ("relative/SKILL.md", "  ", None):
        with pytest.raises(ValueError):
            parse_capability_management_intent(
                {**value, "target": {"kind": "SKILL", "skill_path": path}}
            )
    target = {
        "kind": "MCP_CONNECTION",
        "scope": "USER",
        "source_kind": "LOCAL",
        "server_id": " id ",
    }
    assert (
        parse_capability_management_intent(
            {"action": "REMOVE_LOCAL_MCP", "target": target}
        ).to_dict()["target"]
        == target
    )
    for replacement in (
        {**target, "server_id": "  "},
        {**target, "kind": "MCP_SERVER"},
        {"kind": "MCP_SERVER", "runtime_server_id": "id"},
    ):
        with pytest.raises(ValueError):
            parse_capability_management_intent(
                {"action": "REMOVE_LOCAL_MCP", "target": replacement}
            )


def test_install_destination_and_options_are_separate_from_result_identity():
    value = {
        "action": "INSTALL_PLUGIN",
        "scope": "WORKSPACE",
        "source_path": "/source/plugin",
        "replace": False,
    }
    assert parse_capability_management_intent(value).to_dict() == value
    for extra in (
        {"replace": 1},
        {"source_path": "relative"},
        {"target": {"kind": "PLUGIN", "scope": "WORKSPACE", "plugin_id": "guessed"}},
    ):
        with pytest.raises(ValueError):
            parse_capability_management_intent({**value, **extra})


def test_local_config_null_does_not_mean_open_editor():
    value = {
        "action": "ADD_LOCAL_MCP",
        "target": {
            "kind": "MCP_CONNECTION",
            "scope": "USER",
            "source_kind": "LOCAL",
            "server_id": "docs",
        },
    }
    assert "config" not in parse_capability_management_intent(value).to_dict()
    with pytest.raises(ValueError):
        parse_capability_management_intent({**value, "config": None})
