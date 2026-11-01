"""User-owned MCP templates share real SDK connections, never model tool dispatch."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp import types
from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog
from pulsara_agent.capability.source_query import CapabilityQueryError, capability_target_schema, parse_query
from pulsara_agent.conversation_kernel.capability_query import CapabilitySourceQuery
from pulsara_agent.conversation_kernel.mcp.supervisor import user_prompt_text
from pulsara_agent.identity import derive_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from tests.test_mcp_rpc_errors_and_pagination import _runtime

TARGET = {"kind": "MCP_PROMPT", "server_id": "probe", "name": "first"}


def test_prompt_metadata_target_is_queryable_and_body_tool_is_removed():
    assert "get_mcp_prompt" not in {entry.descriptor.name for entry in builtin_tool_catalog()}
    assert parse_query({"kind": "MCP_PROMPT"}) == {"kind": "MCP_PROMPT"}
    assert parse_query({"target": TARGET}) == {"target": TARGET}
    assert capability_target_schema("MCP_PROMPT")["properties"]["kind"]["const"] == "MCP_PROMPT"


@pytest.mark.parametrize("messages,state", [
    ([], "empty"),
    ([{"role": "user", "content": {"type": "text", "text": "  "}}], "empty"),
    ([{"role": "assistant", "content": {"type": "text", "text": "answer"}}], "unsupported_content"),
    ([{"role": "user", "content": {"type": "image", "data": "AA==", "mimeType": "image/png"}}], "unsupported_content"),
    ([{"role": "user", "content": {"type": "resource", "resource": {"uri": "fixture://a", "text": "data"}}}], "unsupported_content"),
])
def test_unsupported_message_is_never_partially_inserted(messages, state):
    result = user_prompt_text(types.GetPromptResult(messages=messages))
    assert result["state"] == state
    assert "text" not in result


def test_plain_user_messages_preserve_order_and_whitespace():
    result = types.GetPromptResult(messages=[
        {"role": "user", "content": {"type": "text", "text": "  first\n"}},
        {"role": "user", "content": {"type": "text", "text": "second  "}},
    ])
    assert user_prompt_text(result) == {"state": "ready", "text": "  first\n\n\nsecond  "}


@pytest.mark.parametrize("scope_policy", ["ROOT_ONLY", "ROOT_AND_SUBAGENTS"])
def test_real_sdk_user_listing_read_and_missing_target(tmp_path, scope_policy):
    async def exercise():
        async with _runtime(tmp_path, scope_policy=scope_policy) as (supervisor, runtime):
            for scope in (ModelInputScopeKind.ROOT, ModelInputScopeKind.SUBAGENT):
                plan = SimpleNamespace(mcp_catalog_route_projection=SimpleNamespace(routes=()))

                def owner():
                    return CapabilitySourceQuery(None, runtime=runtime, plan=plan, scope=scope, code_direct_specs=())

                page = await owner().execute(parse_query({"kind": "MCP_PROMPT", "limit": 1}))
                if scope is ModelInputScopeKind.SUBAGENT and scope_policy == "ROOT_ONLY":
                    assert page["items"] == []
                    with pytest.raises(CapabilityQueryError) as error:
                        await owner().execute(parse_query({"target": TARGET}))
                    assert error.value.code == "CAPABILITY_NOT_FOUND"
                    continue
                assert page["items"][0]["target"] == TARGET
                detail = await owner().execute(parse_query({"target": TARGET}))
                assert set(detail) == {"target", "source", "server_id", "name", "description", "arguments"}
                assert "invocation" not in detail and "messages" not in detail
                server = {"kind": "MCP_SERVER", "runtime_server_id": derive_id("mcp-server", "runtime-target", "probe")}
                server_detail = await owner().execute(parse_query({"target": server}))
                assert server_detail["prompt_count"] == 1
                assert server_detail["prompt_listing"] == {"kind": "MCP_PROMPT", "parent": server}
            requests = json.loads((tmp_path / "requests.json").read_text())
            assert all(request["method"] != "prompts/get" for request in requests)
            page = supervisor.list_user_prompts(query="fi", limit=1)
            assert page["state"] == "ready"
            assert page["items"][0]["target"] == TARGET
            assert page["next_offset"] is None
            assert supervisor.list_user_prompts(query="probe/fi")["items"] == page["items"]
            assert supervisor.list_user_prompts(query="absent")["items"] == []
            result = await supervisor.preview_user_prompt(TARGET, {})
            assert result == {"state": "ready", "text": "same connection success"}
            assert (await supervisor.preview_user_prompt(TARGET, {"unknown": "value"}))["state"] == "invalid_arguments"
            assert (await supervisor.preview_user_prompt({**TARGET, "name": "gone"}, {}))["state"] == "unavailable"
            assert all(spec.provider_tool_name != "get_mcp_prompt" for spec in runtime.root_tool_specs)
            slot = runtime.slot_lease_by_server["probe"]._slot
            assert slot.active_operation_count == slot.active_dispatch_count == 0
    asyncio.run(exercise())


def test_user_preview_shares_lane_cancels_and_releases_without_dispatch(tmp_path):
    async def exercise():
        async with _runtime(tmp_path) as (supervisor, runtime):
            slot = runtime.slot_lease_by_server["probe"]._slot
            original = slot.client.session.get_prompt
            started = asyncio.Event()
            async def blocked(*args, **kwargs):
                started.set()
                await asyncio.Event().wait()
            slot.client.session.get_prompt = blocked
            leases = slot.active_slot_lease_count
            task = asyncio.create_task(supervisor.preview_user_prompt(TARGET, {}))
            await started.wait()
            assert slot.active_operation_count == 1
            assert slot.active_dispatch_count == 0  # no fabricated model tool call
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert slot.active_operation_count == 0
            assert slot.active_slot_lease_count == leases
            slot.client.session.get_prompt = original
            assert (await supervisor.preview_user_prompt(TARGET, {}))["state"] == "ready"
    asyncio.run(exercise())


def test_real_sdk_prompt_arguments_and_pages(tmp_path):
    async def exercise():
        async with _runtime(tmp_path, pagination_method="prompts/list", prompt_arguments=[
            {"name": "code", "required": True}, {"name": "framework", "required": False},
        ]) as (supervisor, _runtime_owner):
            page = supervisor.list_user_prompts(limit=1)
            assert page["next_offset"] == 1
            second = supervisor.list_user_prompts(offset=page["next_offset"], limit=1)
            assert second["items"][0]["name"] == "second"
            assert second["next_offset"] is None
            assert (await supervisor.preview_user_prompt(TARGET, {}))["fields"] == ["code"]
            assert (await supervisor.preview_user_prompt(TARGET, {"code": "   "}))["state"] == "invalid_arguments"
            assert (await supervisor.preview_user_prompt(TARGET, {"code": 7}))["state"] == "invalid_arguments"
            assert (await supervisor.preview_user_prompt(TARGET, {"code": "  a\nb  "}))["state"] == "ready"
            import json
            requests = json.loads((tmp_path / "requests.json").read_text())
            calls = [r for r in requests if r["method"] == "prompts/get"]
            assert len(calls) == 1
            assert calls[0]["params"]["arguments"] == {"code": "  a\nb  "}
    asyncio.run(exercise())


def test_connection_refresh_invalidates_a_preview_in_flight(tmp_path):
    async def exercise():
        async with _runtime(tmp_path) as (supervisor, runtime):
            slot = runtime.slot_lease_by_server["probe"]._slot
            original = slot.client.session.get_prompt
            async def changed(*args, **kwargs):
                result = await original(*args, **kwargs)
                slot.mark_dirty()
                return result
            slot.client.session.get_prompt = changed
            assert (await supervisor.preview_user_prompt(TARGET, {}))["state"] == "unavailable"
            assert slot.active_operation_count == 0
    asyncio.run(exercise())


def test_desktop_prompt_bridge_is_user_owned_and_rechecks_delivery():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from pulsara_agent.desktop_app.renderer_bridge import RendererBridge
    from pulsara_agent.desktop_app.protocol_client import ProtocolBridgeError

    async def exercise():
        connection = SimpleNamespace(host_session_id="host", session_id="session", generation=7)
        session = SimpleNamespace(preview_mcp_prompt=AsyncMock(return_value={"state": "ready", "text": "task"}))
        bridge = object.__new__(RendererBridge)
        bridge.sessions = SimpleNamespace(session_by_host_id=lambda value: session if value == "host" else None)
        bridge._connection = AsyncMock(return_value=connection)
        bridge.require_chat_controller = AsyncMock()
        body = {"session_id": "session", "target": TARGET, "arguments": {}}
        assert await bridge.mcp_prompts("connection", body, preview=True) == {"state": "ready", "text": "task"}
        session.preview_mcp_prompt.assert_awaited_once_with(TARGET, {})
        assert bridge.require_chat_controller.await_count == 2
        bridge.require_chat_controller.reset_mock()
        bridge.require_chat_controller.side_effect = [None, ProtocolBridgeError("CHAT_CONTROLLER_REQUIRED", "taken over")]
        with pytest.raises(ProtocolBridgeError):
            await bridge.mcp_prompts("connection", body, preview=True)
        with pytest.raises(ValueError):
            bridge.require_chat_controller.side_effect = None
            await bridge.mcp_prompts("connection", {**body, "tool_call_id": "not a tool call"}, preview=True)
    asyncio.run(exercise())
