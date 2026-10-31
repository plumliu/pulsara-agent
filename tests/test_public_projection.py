"""Protocol syntax is compiled; external data never inherits that trust."""

import json
from types import SimpleNamespace

import pytest

from pulsara_agent.capability.public_projection import public_mcp_config
from pulsara_agent.cli import (
    _PluginCliOutput,
    _plugin_cli_print,
    _public_plugin_cli_payload,
)
from pulsara_agent.conversation_kernel.capability_management_execution import (
    _public_management_result,
)
from pulsara_agent.identity import random_id
from pulsara_agent.process_credential_boundary import (
    CredentialObjectKeyConflict,
    ProcessCredentialBoundary,
    ProcessCredentialScrubSet,
)
from pulsara_agent.public_projection import public_record


@pytest.mark.parametrize(
    "secret", ["package", "visibility", "WITHHELD", "APPLIED", "USER", "status"]
)
def test_fixed_protocol_survives_matching_secret_while_dynamic_text_is_scrubbed(secret):
    scrub = ProcessCredentialScrubSet()
    scrub.observe(secret)
    package = random_id("package")
    raw = {
        "status": "APPLIED",
        "action": "INSTALL_PLUGIN",
        "adoption": "NOT_APPLICABLE",
        "target": {"kind": "PLUGIN", "scope": "USER", "plugin_id": "prefix-" + secret},
        "current": {
            "package_install_id": package,
            "operation_status": "INSTALLED",
            "package_root": "prefix-" + secret,
        },
    }
    result = _public_management_result(raw, scrub)
    assert result["status"] == "APPLIED"
    assert result["target"]["scope"] == "USER"
    assert result["target"]["kind"] == "PLUGIN"
    assert secret not in result["target"]["plugin_id"]
    assert secret not in result["current"]["package_root"]
    assert result["current"]["package_install_id"] == (
        {"visibility": "WITHHELD"}
        if secret == "package"
        else {"visibility": "VISIBLE", "value": package}
    )
    assert raw["current"]["package_install_id"] == package
    assert raw["current"]["package_root"] == "prefix-" + secret


def test_external_json_checks_keys_and_values_even_when_named_like_protocol():
    scrub = ProcessCredentialScrubSet()
    scrub.observe("status")
    with pytest.raises(CredentialObjectKeyConflict):
        scrub.scrub_json({"status": "external"})
    raw = {
        "status": "APPLIED",
        "description": "prefix-status-suffix",
        "input_schema": {"properties": {"status": {"type": "string"}}},
        "other": {"ordinary": "prefix-status-suffix"},
    }
    result = public_record(
        raw, scrub, fixed=("status",), external=("input_schema", "other")
    )
    assert result["status"] == "APPLIED"
    assert result["withheld_fields"] == ["input_schema"]
    assert "input_schema" not in result
    assert result["other"] == {"ordinary": "prefix-[REDACTED_CREDENTIAL]-suffix"}
    assert raw["input_schema"]["properties"]["status"] == {"type": "string"}


def test_hook_source_external_environment_uses_same_block_boundary():
    from pulsara_agent.hooks.executor import HookSecretScrubSet
    from pulsara_agent.plugins.public_projection import project_hook_public

    scrub = HookSecretScrubSet()
    scrub.observe("HOME")
    value = project_hook_public(
        {
            "scope": "user",
            "source_disposition": "COMPLETE",
            "declaration_environment": {"HOME": "/private", "REGION": "west"},
        },
        scrub,
    )
    assert value == {
        "scope": "user",
        "source_disposition": "COMPLETE",
        "withheld_fields": ["declaration_environment"],
    }


@pytest.mark.parametrize("secret", ["stdio", "none", "scope_policy"])
def test_connection_fixed_enums_survive_but_external_maps_are_not_exempt(secret):
    scrub = ProcessCredentialScrubSet()
    scrub.observe(secret)
    value = {
        "transport": {
            "type": "stdio",
            "command": "prefix-" + secret,
            "args": [],
            "env": {secret: "value"},
            "secret_env": {},
        },
        "auth": {"type": "none"},
        "scope_policy": "ROOT_ONLY",
        "public_headers": {},
    }
    result = public_mcp_config(value, scrub)
    assert result["transport"]["type"] == "stdio"
    assert result["auth"]["type"] == "none"
    assert result["scope_policy"] == "ROOT_ONLY"
    assert secret not in result["transport"]["command"]
    assert "env" not in result["transport"]
    assert result["transport"]["withheld_fields"] == ["env"]


def test_cli_projects_under_sink_gate_after_rotation_without_scrubbing_fixed_json(
    capsys,
):
    boundary = ProcessCredentialBoundary("old-secret")
    scrub = boundary.capture_scrub_set()
    output = _PluginCliOutput(
        {
            "operation": "install_local_plugin",
            "disposition": "INSTALLED",
            "source_path": "old-secret/status",
            "package_install_id": random_id("package"),
        },
        True,
        scrub,
    )
    boundary.rotate_sync("status")
    _plugin_cli_print(output, boundary)
    value = json.loads(capsys.readouterr().out)
    assert value["operation"] == "install_local_plugin"
    assert value["disposition"] == "INSTALLED"
    assert (
        "old-secret" not in value["source_path"]
        and "status" not in value["source_path"]
    )
    boundary.rotate_sync("package")
    _plugin_cli_print(output, boundary)
    assert json.loads(capsys.readouterr().out)["package_install_id"] == {
        "visibility": "WITHHELD"
    }


def test_cli_keeps_committed_disposition_when_one_external_block_is_withheld():
    scrub = ProcessCredentialScrubSet()
    scrub.observe("package")
    value = _public_plugin_cli_payload(
        {
            "operation": "install_local_plugin",
            "disposition": "ACK_UNKNOWN",
            "attempted_package_install_id": random_id("package"),
            "last_known_cut": "STATE_RENAME_FULL_CONFIRMATION_UNAVAILABLE",
            "summary": {
                "manifest": {"name": "example", "author": None},
                "skills": {
                    "disposition": "MISSING",
                    "definitions": [],
                    "diagnostics": [],
                },
                "hooks": {
                    "disposition": "MISSING",
                    "definitions": [],
                    "diagnostics": [],
                },
                "mcp": {
                    "disposition": "VALID",
                    "diagnostics": [],
                    "servers": [
                        {
                            "server_id": "example",
                            "transport": "stdio",
                            "command": "node",
                            "env": {"package_install_id": "external"},
                        }
                    ],
                },
            },
        },
        scrub,
    )
    assert value["disposition"] == "ACK_UNKNOWN"
    assert value["last_known_cut"] == "STATE_RENAME_FULL_CONFIRMATION_UNAVAILABLE"
    assert value["attempted_package_install_id"] == {"visibility": "WITHHELD"}
    assert value["summary"]["mcp"]["servers"][0]["withheld_fields"] == ["env"]


def test_provider_diagnostics_keep_protocol_and_refuse_only_conflicting_external_body(
    caplog,
):
    from pulsara_agent.llm.adapters.openai.retrying import (
        log_invalid_tool_arguments,
        log_provider_decision,
    )
    from pulsara_agent.llm.retry import LLMRetryConfig

    scrub = ProcessCredentialScrubSet()
    scrub.observe("package")
    error = RuntimeError("prefix-package-suffix")
    error.body = {"package_install_id": "external"}
    log_provider_decision(
        api="openai_chat_completions",
        model=SimpleNamespace(
            route_id="fixture", id="fixture", base_url="https://example.invalid"
        ),
        scrub=scrub,
        config=LLMRetryConfig(),
        attempt=1,
        max_attempts=6,
        phase="stream",
        exc=error,
        decision=None,
        skipped_reason=None,
        semantic=False,
        terminal=False,
        closed=True,
    )
    value = json.loads(
        caplog.records[-1].getMessage().removeprefix("Provider request ")
    )
    assert value["phase"] == "stream"
    assert value["decision"] == "failed"
    assert value["exception_chain"][0]["withheld_fields"] == ["body"]
    assert "package" not in value["exception_chain"][0]["message"]
    log_invalid_tool_arguments(
        scrub=scrub,
        attempt=2,
        arguments={"call": "package"},
        terminal_chunk={"package_install_id": "external"},
        exc=error,
    )
    value = json.loads(caplog.records[-1].getMessage().split(" diagnostic=", 1)[1])
    assert value["withheld_fields"] == ["terminal_chunk"]
    assert value["arguments"] == {"call": "[REDACTED_CREDENTIAL]"}
