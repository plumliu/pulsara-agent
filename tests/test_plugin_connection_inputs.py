import asyncio
import json
from dataclasses import replace

import pytest

from pulsara_agent.plugins.connection_inputs import (
    parse_connection_inputs,
    connection_input_defaults,
)
from pulsara_agent.mcp_credentials import (
    McpCredentialOwner,
    McpCredentialMissing,
    resolve_secret,
)
from pulsara_agent.plugins.mcp_connection import resolve_connection_overlay
from pulsara_agent.plugins.contracts import PluginMcpHttpSummary


def definition():
    return {
        "servers": {
            "docs": {
                "inputs": [
                    {
                        "name": "TOKEN",
                        "title": "API key",
                        "private": True,
                        "required": True,
                        "default": None,
                    },
                ],
                "targets": [
                    {
                        "kind": "header",
                        "name": "Authorization",
                        "parts": [{"literal": "Bearer "}, {"input": "TOKEN"}],
                    }
                ],
            }
        }
    }


def test_private_input_is_a_missing_typed_reference_not_a_literal_or_noauth():
    inputs = parse_connection_inputs(definition(), server_ids=("docs",))["docs"]
    owner = McpCredentialOwner("plugin", "user", "docs", "fixture")
    overlay = connection_input_defaults(
        inputs, owner=owner, transport_kind="streamable_http"
    )
    name, value = overlay.auth.headers[0]
    assert name == "Authorization"
    with pytest.raises(McpCredentialMissing):
        resolve_secret(value)
    assert (
        resolve_secret(
            value,
            lambda binding: (
                "private-value"
                if binding.owner == owner and binding.name == "input:TOKEN"
                else None
            ),
        )
        == "Bearer private-value"
    )
    assert "private-value" not in repr(inputs)


@pytest.mark.parametrize(
    "mutation",
    [
        "secret-default",
        "endpoint-secret",
        "executable",
        "unknown-server",
        "undefined",
        "duplicate-target",
    ],
)
def test_input_definition_rejects_incompatible_behavior(mutation):
    raw = definition()
    component = raw["servers"]["docs"]
    if mutation == "secret-default":
        component["inputs"][0]["default"] = "must-not-publish"
    elif mutation == "endpoint-secret":
        component["targets"][0].update(kind="endpoint", name="endpoint")
    elif mutation == "executable":
        component["targets"][0].update(kind="command", name="command")
    elif mutation == "unknown-server":
        raw["servers"]["other"] = raw["servers"].pop("docs")
    elif mutation == "undefined":
        component["targets"][0]["parts"][1]["input"] = "OTHER"
    else:
        component["targets"].append(component["targets"][0])
    with pytest.raises(ValueError):
        parse_connection_inputs(raw, server_ids=("docs",))


def test_replaced_input_definition_cannot_keep_obsolete_instance_reference():
    inputs = parse_connection_inputs(definition(), server_ids=("docs",))["docs"]
    owner = McpCredentialOwner("plugin", "user", "docs", "fixture")
    old = connection_input_defaults(
        inputs, owner=owner, transport_kind="streamable_http"
    )
    server = PluginMcpHttpSummary(
        "docs", "https://example.org/mcp", (), connection_inputs=inputs
    )
    assert resolve_connection_overlay(server, old, owner=owner).auth == old.auth
    with pytest.raises(ValueError, match="immutable"):
        resolve_connection_overlay(
            replace(server, connection_inputs=type(inputs)()), old, owner=owner
        )


def test_package_observation_and_installed_inspection_keep_input_definition(tmp_path):
    from tests.test_capability_management_preparation import preparation, install_plugin
    from pulsara_agent.plugins.contracts import (
        InstallLocalPluginRequest,
        PluginScopeKind,
        InspectLocalPluginsRequest,
    )
    from pulsara_agent.plugins.mcp_adapter import materialize_plugin_mcp_definition
    from time import monotonic

    async def run():
        service = preparation(tmp_path)
        installed = await install_plugin(service, tmp_path)
        source = tmp_path / "source"
        inputs = source / "dev.pulsara" / "mcp" / "connection-inputs.json"
        inputs.parent.mkdir(parents=True)
        inputs.write_text(json.dumps(definition()))
        replacement = await service.plugins.install_local_plugin(
            InstallLocalPluginRequest(
                source, PluginScopeKind.USER, monotonic() + 10, replace=True
            ),
            connections=service.mcp,
        )
        assert replacement.disposition.value == "REPLACED", replacement
        inspection = service.plugins.inspect_local_plugins(
            InspectLocalPluginsRequest(monotonic() + 10)
        )
        try:
            instance = inspection.instances[0]
            assert (
                instance.summary.mcp.mcp_servers[0].connection_inputs.inputs[0].name
                == "TOKEN"
            )
            store = service.plugins._store()
            config = materialize_plugin_mcp_definition(
                identity=installed.identity,
                state=store.read_state(store.layout(installed.identity)),
                server=instance.summary.mcp.mcp_servers[0],
                package_root=instance.package_root,
                data_root=instance.data_root,
            )
            assert config.auth.kind == "static_headers"
            with pytest.raises(McpCredentialMissing):
                resolve_secret(config.auth.headers[0][1], config.secret_resolver)
        finally:
            inspection.close()
            await service.mcp.aclose()

    asyncio.run(run())
