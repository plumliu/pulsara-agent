"""Unavailable-tool guidance after real SDK catalog refreshes."""

import asyncio
import json
from types import SimpleNamespace

from mcp.server.lowlevel import Server
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ToolsListChanged
import mcp_types as types
import pytest

from pulsara_agent.capability.contracts import PreparedUnavailableDirectMcpGate
from pulsara_agent.primitives.context import canonical_json_bytes
from tests import test_mcp_schema_validation as fixture
from tests import test_round6_mcp_production as production
from tests.test_mcp_catalog_subscriptions import _sdk_http, _supervisor


@pytest.mark.parametrize("change", ["schema", "removed"])
def test_public_guidance_distinguishes_schema_change_and_missing_connection_tool(tmp_path, change):
    async def scenario():
        bus = InMemorySubscriptionBus()
        handler = ListenHandler(bus)
        revision = 1

        async def tools(_ctx, _params):
            return types.ListToolsResult(tools=(
                [] if change == "removed" and revision == 2 else [types.Tool(
                    name="editable_tool",
                    input_schema={
                        "type": "object",
                        "properties": {"value": {"type": "string" if revision == 1 else "integer"}},
                    },
                )]
            ))

        server = Server(
            "guidance-catalog", on_list_tools=tools,
            on_subscriptions_listen=handler,
        )
        async with _sdk_http(server) as endpoint:
            supervisor = _supervisor(tmp_path, endpoint)
            port = fixture._port(tmp_path, supervisor)
            borrows = []
            try:
                await supervisor.start()
                port.prepare_tool_surface_safe_point()
                cold = production.prepare_test_direct_tool_surface(port)
                borrows.append(port.borrow_tool_surface(cold))
                retained_plan = cold.capability_exposure_plan
                frozen_native_tools = tuple(
                    canonical_json_bytes(item.wire_tool)
                    for item in retained_plan.direct_projection_set.projections
                )
                name = "mcp__catalog__editable_tool"
                assert name in {item.name for item in cold.model_surface.tool_specs}

                async def refresh():
                    await bus.publish(ToolsListChanged())
                    async with asyncio.timeout(5):
                        while "catalog" not in supervisor._pending:
                            await asyncio.sleep(0.01)
                    port.prepare_tool_surface_safe_point()

                revision = 2
                await refresh()
                # Rebind the existing epoch's exact immutable native plan to
                # the newly discovered physical runtime. No cold adoption or
                # new provider roots are requested by this operation.
                current = port.prepare_planned_tool_surface(
                    plan=retained_plan,
                    builtin=port.sealed_builtin_capability_snapshot(
                        conversation_scope_kind=production.ModelInputScopeKind.ROOT,
                        scope_subagent_task_id=None,
                    ),
                )
                borrow = port.borrow_tool_surface(current)
                borrows.append(borrow)
                gate = borrow.execution_binding(name)
                assert isinstance(gate, PreparedUnavailableDirectMcpGate)
                assert gate.unavailable_reason_code == (
                    "SCHEMA_REPLACED_PENDING_COLD_ADOPTION"
                    if change == "schema" else "MCP_DIRECT_CURRENTLY_UNAVAILABLE"
                )
                assert current.model_surface is cold.model_surface
                assert current.capability_exposure_plan.direct_projection_set is retained_plan.direct_projection_set
                assert tuple(
                    canonical_json_bytes(item.wire_tool)
                    for item in current.capability_exposure_plan.direct_projection_set.projections
                ) == frozen_native_tools

                permission = production.build_run_permission_snapshot(
                    snapshot_id="permission:xe33u4flpejjy72jmvg5arcvtu",
                    requested_mode=production.PermissionMode.BYPASS_PERMISSIONS,
                    effective_mode=production.PermissionMode.BYPASS_PERMISSIONS,
                    admission_source=production.RunPermissionAdmissionSource.USER_SUBMISSION,
                )
                prepared = port.prepare_resolved_invocation(
                    tool_name=name, arguments={"value": "old schema"}, surface_borrow=borrow,
                )
                authorization = await port.authorize(
                    tool_name=name, arguments={"value": "old schema"},
                    tool_call_id="call:unavailable", turn_id="turn:fugd2z4wkqsu77svw4oie5ktvy",
                    assistant_entry_id="entry:5qidalqvkujk5ykzzzfpwie2qy",
                    permission_snapshot=permission, surface_borrow=borrow,
                    memory_context=production._enabled_memory_context(),
                )
                assert prepared.authorization.kind is production.KernelToolAuthorizationKind.TOOL_UNAVAILABLE
                assert authorization.kind is production.KernelToolAuthorizationKind.TOOL_UNAVAILABLE
                message = authorization.public_message
                assert prepared.authorization.public_message == message
                assert "Do not route" in message and "use_new_mcp_tool" in message
                if change == "schema":
                    assert "different schema" in message
                    assert "Reconnecting or reloading capabilities cannot adopt" in message
                    assert "new cold context" in message
                    assert "explicitly adopted compaction successor" in message
                    assert "same-schema reconnect" not in message
                    assert "server connection is currently unavailable" not in message
                else:
                    assert "same-schema connection is available" in message
                    assert "list_capabilities" in message
                    assert "new cold context" not in message

                inspected = port._inspect_mcp_tool_result(
                    target={"kind": "MCP_TOOL", "server_id": "catalog", "tool_name": name},
                    invocation_context=SimpleNamespace(surface_borrow=borrow),
                )
                assert json.loads(inspected.content)["diagnostics"][0]["message"] == message

                if change == "removed":
                    revision = 1
                    await refresh()
                    restored = port.prepare_planned_tool_surface(
                        plan=retained_plan,
                        builtin=port.sealed_builtin_capability_snapshot(
                            conversation_scope_kind=production.ModelInputScopeKind.ROOT,
                            scope_subagent_task_id=None,
                        ),
                    )
                    restored_borrow = port.borrow_tool_surface(restored)
                    borrows.append(restored_borrow)
                    assert not isinstance(restored_borrow.execution_binding(name), PreparedUnavailableDirectMcpGate)
                    assert restored.model_surface is cold.model_surface
            finally:
                for borrow in reversed(borrows):
                    borrow.close()
                supervisor.stop_admission()
                close = asyncio.create_task(supervisor.aclose())
                await port.aclose(timeout_seconds=5)
                await close

    asyncio.run(scenario())
