"""The device inventory observes loaded owners, without choosing a conversation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from pulsara_agent import mcp_config
from pulsara_agent.desktop_app import session_controller as controller_module
from pulsara_agent.desktop_app.session_controller import LocalSessionController


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "mcp.yaml"
    path.write_text(
        "servers:\n  docs:\n    transport:\n"
        "      type: streamable_http\n      endpoint: https://docs.example/mcp\n"
    )
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path))
    monkeypatch.setattr(controller_module, "_user_plugins_payload", lambda *_: {})
    return mcp_config.load_mcp_server_configs(user_config_path=path)[0]


def inspection(config, status="READY", names=("search",), *, resources=0,
               source="USER", matches=True, identity=None, instructions=""):
    return SimpleNamespace(
        mcp_catalog=SimpleNamespace(servers=(SimpleNamespace(
            server_id=config.server_id,
            status=SimpleNamespace(value=status),
            exposed_tool_count=len(names),
            resource_count=resources,
            resource_template_count=0,
            prompt_count=0,
            sanitized_instructions=instructions,
            stable_failure_category="MCPError" if status.startswith("FAILED") else None,
        ),)),
        mcp_configured_servers=(SimpleNamespace(
            server_id=config.server_id,
            resolved_config_identity=identity or config.resolved_config_identity,
            source_kind=source,
            status_matches_config=matches,
        ),),
        mcp_tools=tuple(SimpleNamespace(
            server_id=config.server_id, provider_name=name, remote_name=name,
            description=name, effect="READ_ONLY", available_to_subagents=False,
            parallel_safe=False,
        ) for name in names),
    )


def project(config, *observations):
    return controller_module._user_capability_payload(
        skills={}, mcp_configs=(config,), plugins=object(),
        secret_resolver=lambda _: None, live_inspections=observations,
    )["mcp"]["servers"][0]


def test_ready_owner_wins_and_directory_stays_coherent(config):
    ready = inspection(config, names=("search", "read"), resources=2,
                       instructions="ready owner")
    failed = inspection(config, "FAILED_RETRYABLE", ("failed-only",), resources=12)
    discovering = inspection(config, "DISCOVERING", ("pending-only",))
    smaller = inspection(config, names=("other-ready-only",), resources=20)
    forward = project(config, failed, discovering, ready, smaller)
    reverse = project(config, smaller, ready, discovering, failed)
    assert forward == reverse
    assert forward["status"] == "READY"
    assert forward["tool_count"] == 2
    assert [tool["name"] for tool in forward["tools"]] == ["search", "read"]
    assert forward["resource_count"] == 2
    assert forward["instructions"] == "ready owner"
    assert forward["has_failure"] is False


@pytest.mark.parametrize("foreign", [
    {"source": "WORKSPACE"},
    {"source": "MANAGED_PACKAGE"},
    {"identity": "old-user-config"},
    {"matches": False},
])
def test_foreign_or_stale_owner_cannot_override_current_user_config(config, foreign):
    failed = inspection(config, "FAILED_RETRYABLE", ())
    result = project(config, inspection(config, **foreign), failed)
    assert result["status"] == "FAILED_RETRYABLE"
    assert result["tool_count"] == 0
    assert result["tools"] == []
    assert project(config, inspection(config, **foreign))["status"] == "CONFIGURED"


def test_disabled_current_config_wins_over_loaded_observation(config, tmp_path):
    path = tmp_path / "disabled.yaml"
    entry = controller_module.config_to_entry(config)
    entry["enabled"] = False
    path.write_text(yaml.safe_dump({"servers": {config.server_id: entry}}))
    disabled = mcp_config.load_mcp_server_configs(user_config_path=path)[0]
    result = project(disabled, inspection(config))
    assert result["status"] == "DISABLED"
    assert result["tools"] == []


def test_inventory_reads_all_loaded_owners_and_drops_retired_owner(config, monkeypatch):
    async def exercise():
        monkeypatch.setattr(controller_module, "_inspect_user_skills", lambda: {})
        controller = object.__new__(LocalSessionController)
        controller._lock = asyncio.Lock()
        controller._operations = {}
        controller.core = SimpleNamespace(
            mcp_management=SimpleNamespace(
                load_configs=lambda: (config,),
                settings=SimpleNamespace(read=lambda: SimpleNamespace(mcp_secret=lambda _: None)),
            ),
            inspect_user_plugins=AsyncMock(return_value=object()),
        )
        current = inspection(config, "FAILED_RETRYABLE", ())
        ready = inspection(config, names=("search", "read"))
        controller._by_session = {
            "session:old": SimpleNamespace(session=SimpleNamespace(inspect_capability_catalog=lambda: current)),
            "session:new": SimpleNamespace(session=SimpleNamespace(inspect_capability_catalog=lambda: ready)),
        }

        async def read():
            payload = await controller.inspect_user_capabilities()
            return payload["mcp"]["servers"][0]

        assert (await read())["status"] == "READY"
        # Map insertion/selection order cannot choose the observed conversation.
        controller._by_session = dict(reversed(tuple(controller._by_session.items())))
        assert (await read())["tool_count"] == 2
        # Lifecycle owners already quiescing are unavailable even before unpublish.
        controller._operations["session:new"] = object()
        result = await read()
        assert result["status"] == "FAILED_RETRYABLE"
        assert result["tools"] == []
        controller._by_session.clear()
        result = await read()
        assert result["status"] == "CONFIGURED"
        assert result["tools"] == []

    asyncio.run(exercise())
