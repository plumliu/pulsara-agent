"""Management authorization through the production Host and PostgreSQL."""

from __future__ import annotations

import asyncio
import json

import pytest

from pulsara_agent.capability.mcp_management import LocalMcpTarget
from pulsara_agent.conversation_kernel.tool_outputs import PostgresToolOutputReadPort
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import MessageRole, PromptContent, join_text_content
from pulsara_agent.plugins.contracts import InstallLocalPluginRequest, PluginScopeKind
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.primitives.permission import PermissionMode
from tests.support.capability_management_targets import mcp_target, plugin_target
from tests.support.plugin_review import enable_reviewed_plugin
from tests.test_blocking_steer_host import _wait_for_card
from tests.test_round4_plan_host import _open_test_session
from tests.test_tool_permission_settlement import (
    ApprovalModel,
    _accepted_result,
    _decide,
)


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "permission",
    [PermissionMode.ASK_PERMISSIONS, PermissionMode.READ_ONLY],
)
def test_plugin_disables_only_after_the_current_user_interaction(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
    permission,
):
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "source"
    skill = source / "skills" / "fixture-skill"
    skill.mkdir(parents=True)
    plugin_id = "fixture-plugin"
    (source / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": plugin_id,
            }
        ),
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(
        "---\nname: fixture-skill\ndescription: A local fixture skill.\n---\n\nFixture.\n",
        encoding="utf-8",
    )

    async def scenario():
        model = ApprovalModel("manage_capability", {})
        core, session = await _open_test_session(
            tmp_path=workspace,
            model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )
        running = None
        try:
            management = session._tools._capability_management
            installed = await management.plugins.install_local_plugin(
                InstallLocalPluginRequest(
                    source,
                    PluginScopeKind.USER,
                    management.deadline(),
                ),
                connections=management.mcp,
            )
            assert installed.disposition.value == "INSTALLED"
            await asyncio.to_thread(
                enable_reviewed_plugin,
                management.plugins,
                installed,
                management.deadline(),
            )
            initial_plugin = await management._plugin(None, plugin_id)
            assert initial_plugin.enabled

            target = plugin_target(management, plugin_id)
            model.arguments = {
                "action": "SET_PLUGIN_ENABLED",
                "target": target,
                "enabled": False,
            }
            prepared_calls, invoked_calls = [], []
            authorize, invoke = session._tools.authorize, session._tools.invoke

            async def observe_authorization(**kwargs):
                authorization = await authorize(**kwargs)
                if kwargs["tool_name"] == "manage_capability":
                    prepared_calls.append(authorization.capability_call)
                return authorization

            async def observe_invocation(**kwargs):
                if kwargs["tool_name"] == "manage_capability":
                    invoked_calls.append(kwargs["invocation_context"].capability_call)
                return await invoke(**kwargs)

            monkeypatch.setattr(session._tools, "authorize", observe_authorization)
            monkeypatch.setattr(session._tools, "invoke", observe_invocation)

            controller = random_id("attachment")
            assert await session.attach_controller(controller)
            running = asyncio.create_task(
                session.run_turn(
                    PromptContent.text("Disable the fixture plugin."),
                    requested_permission_mode=permission,
                )
            )
            await _wait_for_card(session)
            snapshot = session.live_control.current_snapshot()
            card = snapshot.current_interaction
            assert card is not None
            assert (await management._plugin(None, plugin_id)).enabled
            assert not invoked_calls
            (prepared_call,) = prepared_calls
            assert prepared_call is not None
            assert thaw_json(prepared_call.prepared.intent.target) == target

            if permission is PermissionMode.ASK_PERMISSIONS:
                assert card.interaction_kind == "TOOL_CONFIRMATION"
                assert card.public_options == ("ALLOW", "DENY")
                assert card.public_prompt.startswith("停用插件")
                assert target["plugin_path"] in card.public_prompt
                accepted = await _decide(session, controller, "ALLOW")
                assert accepted.status == "SUCCEEDED"
                assert accepted.public_code == "INTERACTION_ALLOW"
            else:
                assert card.interaction_kind == "CAPABILITY_FORM"
                assert card.public_options == ("SUBMIT", "CANCEL")
                authority = {
                    "attachment_id": controller,
                    "interaction_id": card.interaction_id,
                    "expected_owner_epoch": snapshot.owner_epoch,
                    "expected_live_revision": snapshot.revision,
                }
                form = thaw_json(session.read_capability_form(**authority))
                assert form["prefill"]["target"] == target
                assert form["prefill"]["enabled"] is False
                await session.resolve_capability_form(
                    **authority,
                    decision="SUBMIT",
                    submission={},
                )

            await asyncio.wait_for(running, 5)
            (invoked_call,) = invoked_calls
            assert invoked_call is prepared_call
            current = await management._plugin(None, plugin_id)
            assert current.identity == initial_plugin.identity
            assert current.package_install_id == initial_plugin.package_install_id
            assert current.enabled is False
            assert session.live_control.current_snapshot().current_interaction is None

            row = await _accepted_result(session)
            assert row["result_state"] == "SUCCESS"
            assert row["result_origin_kind"] == "PHYSICAL_ATTEMPT"
            assert row["attempt_id"] is not None
            assert row["attempts"] == [row["attempt_id"]]
            assert row["decisions"] == (
                ["ALLOW"] if permission is PermissionMode.ASK_PERMISSIONS else []
            )
            output = PostgresToolOutputReadPort(
                session.repository.connection_provider,
                session_id=session.session_id,
                workspace_id=session.workspace.workspace_key,
            )
            result = output.read_result(
                {
                    "kind": "SESSION_ENTRY",
                    "entry_id": row["result_entry_id"],
                }
            ).value
            text = f"已停用插件 {plugin_id}。"
            assert result == {
                "target": target,
                "meta": {"status": "SUCCESS"},
                "content": ({"type": "text", "value": text},),
            }
            (delivered,) = [
                message
                for message in model.requests[-1].compiled_input.messages
                if message.role is MessageRole.TOOL_RESULT
                and message.tool_call_id == "call:permission"
            ]
            native_result = join_text_content(delivered.content)
            assert "\nmeta:\n  status: SUCCESS\n" in native_result
            assert text in native_result
            assert f'"entry_id":"{row["result_entry_id"]}"' in native_result
        finally:
            await core.shutdown()
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("permission", "has_config", "needs_form"),
    [
        pytest.param(PermissionMode.ASK_PERMISSIONS, True, True, id="ask-edit-prefill"),
        pytest.param(
            PermissionMode.BYPASS_PERMISSIONS, True, False, id="bypass-direct"
        ),
        pytest.param(
            PermissionMode.BYPASS_PERMISSIONS, False, True, id="bypass-fill-form"
        ),
    ],
)
def test_add_mcp_persists_the_address_selected_by_its_authorization_path(
    tmp_path,
    monkeypatch,
    stage2_migrated_postgres_database,
    permission,
    has_config,
    needs_form,
):
    monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = LocalMcpTarget("docs", workspace)
    proposed_url = "https://docs-test.example.org/mcp"
    submitted_url = "https://docs.example.org/mcp"
    proposed = {"transport": {"type": "streamable_http", "endpoint": proposed_url}}

    async def scenario():
        model = ApprovalModel("manage_capability", {})
        core, session = await _open_test_session(
            tmp_path=workspace,
            model=model,
            postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
            monkeypatch=monkeypatch,
        )
        running = None
        try:
            management = session._tools._capability_management
            target = mcp_target(management, server.server_id, scope="WORKSPACE")
            model.arguments = {"action": "ADD_LOCAL_MCP", "target": target}
            if has_config:
                model.arguments["config"] = proposed

            if not needs_form:

                async def unexpected_interaction(**_kwargs):
                    pytest.fail("A complete bypass candidate opened an interaction")

                monkeypatch.setattr(
                    session._interactions,
                    "request_tool_confirmation",
                    unexpected_interaction,
                )
                monkeypatch.setattr(
                    session._interactions,
                    "request_capability_form",
                    unexpected_interaction,
                )
            controller = random_id("attachment")
            assert await session.attach_controller(controller)
            running = asyncio.create_task(
                session.run_turn(
                    PromptContent.text("Add the docs MCP connection."),
                    requested_permission_mode=permission,
                )
            )
            if needs_form:
                await _wait_for_card(session)
                snapshot = session.live_control.current_snapshot()
                card = snapshot.current_interaction
                assert card.interaction_kind == "CAPABILITY_FORM"
                assert card.public_options == ("SUBMIT", "CANCEL")
                assert management.mcp.inspect(server) is None
                authority = {
                    "attachment_id": controller,
                    "interaction_id": card.interaction_id,
                    "expected_owner_epoch": snapshot.owner_epoch,
                    "expected_live_revision": snapshot.revision,
                }
                form = thaw_json(session.read_capability_form(**authority))
                assert form["prefill"]["target"] == target
                prefill = form["prefill"]["config"]
                if has_config:
                    assert prefill["transport"]["endpoint"] == proposed_url
                else:
                    assert prefill == {}
                submission = {
                    **prefill,
                    "transport": {"type": "streamable_http", "endpoint": submitted_url},
                }
                await session.resolve_capability_form(
                    **authority,
                    decision="SUBMIT",
                    submission={"config": submission},
                )
            await asyncio.wait_for(running, 5)
            assert session.live_control.current_snapshot().current_interaction is None
            saved = management.mcp.inspect(server)
            assert saved is not None
            assert saved.transport.endpoint == (
                submitted_url if needs_form else proposed_url
            )
            row = await _accepted_result(session)
            assert row["result_state"] == "SUCCESS" and row["attempts"] == [
                row["attempt_id"]
            ]
        finally:
            await core.shutdown()
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())
