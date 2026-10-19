"""Public local OAuth observations join the current saved target and grant."""

import asyncio
from types import SimpleNamespace

import pytest
import yaml

from pulsara_agent.conversation_kernel.mcp.oauth import McpOAuthManager, McpOAuthNeedsLogin
from pulsara_agent.desktop_app.session_controller import LocalSessionController
from tests.test_mcp_oauth_reauthorization import login, revocable_server


def _controller(service):
    controller = object.__new__(LocalSessionController)
    controller.core = SimpleNamespace(mcp_management=service)
    return controller


def _edit_target(path, change):
    raw = yaml.safe_load(path.read_text())
    entry = raw["servers"]["revocable"]
    if change == "endpoint":
        entry["transport"]["endpoint"] += "/replacement"
    elif change == "auth":
        entry["auth"]["scope"] = "replacement-scope"
    elif change == "remove":
        del raw["servers"]["revocable"]
    else:
        raise AssertionError(change)
    path.write_text(yaml.safe_dump(raw))


@pytest.mark.parametrize("change", ["endpoint", "auth", "remove"])
def test_external_target_edit_does_not_report_old_authorization(tmp_path, change):
    async def scenario():
        async with revocable_server(tmp_path) as state:
            old = await login(state)
            controller = _controller(state.service)
            assert (await controller.mcp_authorization("revocable", action="status"))["state"] == "authorized"
            _edit_target(tmp_path / "mcp.yaml", change)
            assert await controller.mcp_authorization("revocable", action="status") == {
                "state": "idle", "error": None,
            }
            assert state.settings.read().mcp_authorization(state.target.owner) == old
            assert state.service.oauth.login_state(state.target.owner)["state"] == "idle"
            config = state.service.inspect(state.target)
            if config is not None:
                assert state.service.oauth.login_state(state.target.owner, config)["state"] == "idle"
                with pytest.raises(McpOAuthNeedsLogin):
                    await config.authorization_provider()
            assert state.browser_visits == state.issued == 1

    asyncio.run(scenario())


def test_pending_flow_is_visible_only_for_its_current_target(tmp_path):
    async def scenario():
        async with revocable_server(tmp_path) as state:
            state.auto_callback = False
            controller = _controller(state.service)
            flow = await state.service.authorize(state.target)
            async with asyncio.timeout(5):
                while flow.state != "awaiting_user":
                    await asyncio.sleep(0.01)
            assert (await controller.mcp_authorization("revocable", action="status"))["state"] == "awaiting_user"
            _edit_target(tmp_path / "mcp.yaml", "endpoint")
            assert (await controller.mcp_authorization("revocable", action="status"))["state"] == "idle"
            # A read does not cancel or adopt the old flow's private draft.
            assert flow.state == "awaiting_user" and not flow.task.done()
            await state.service.oauth.cancel(state.target.owner)

    asyncio.run(scenario())


def test_completed_flow_cannot_replace_a_missing_current_grant(tmp_path):
    async def scenario():
        async with revocable_server(tmp_path) as state:
            old = await login(state)
            controller = _controller(state.service)
            await state.settings.replace_mcp_authorization(state.target.owner, None, expected=old)
            assert (await controller.mcp_authorization("revocable", action="status"))["state"] == "idle"
            config = state.service.inspect(state.target)
            with pytest.raises(McpOAuthNeedsLogin):
                await config.authorization_provider()

    asyncio.run(scenario())


def test_restarted_manager_checks_saved_grant_against_current_target(tmp_path):
    async def scenario():
        async with revocable_server(tmp_path) as state:
            await login(state)
            previous = state.service.oauth
            restarted = McpOAuthManager(
                state.settings, lane=previous.lane,
                credential_boundary=previous.credential_boundary,
                open_browser=previous.open_browser,
            )
            state.service.oauth = restarted
            try:
                controller = _controller(state.service)
                assert (await controller.mcp_authorization("revocable", action="status"))["state"] == "authorized"
                _edit_target(tmp_path / "mcp.yaml", "endpoint")
                assert (await controller.mcp_authorization("revocable", action="status"))["state"] == "idle"
                assert not restarted._logins
            finally:
                await previous.aclose()

    asyncio.run(scenario())
