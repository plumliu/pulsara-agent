"""Real SDK HTTP + PostgreSQL + kernel/compiler + both provider wire adapters."""

import asyncio
from dataclasses import replace
from datetime import timezone
import json
from time import monotonic

from mcp.server.lowlevel import Server
from mcp.server.subscriptions import (
    InMemorySubscriptionBus,
    ListenHandler,
    ToolsListChanged,
)
import mcp_types as types
import pytest

from pulsara_agent.conversation_kernel.context_sources import (
    KernelContextSourceCollector,
)
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import MessageRole, join_text_content
from pulsara_agent.mcp_config import _parse_server
from pulsara_agent.model_input.continuity import decode_runtime_observation
from pulsara_agent.model_input.contracts import (
    ContextSourceKind,
    ModelInputScopeKind,
    StructuredModelInputCompileError,
)
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.ports.provider_stream import (
    freeze_provider_adapter_completed_replay_payload,
)
from pulsara_agent.llm.provider_replay import ProviderAssistantReplayCodecKind
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from tests.support.model_config import (
    acquire_bound_test_writer,
    bind_test_session,
    frozen_test_prompt,
    test_model_runtime as model_runtime,
)
from tests.support.postgres import verified_postgres_provider
from tests.support.postgres_database import (
    admin_root_dsn,
    create_migrated_postgres_test_database,
    drop_postgres_test_database,
)
from tests.support.round3 import CallbackScriptedKernelModel
from tests.test_mcp_catalog_subscriptions import _sdk_http, _supervisor
from tests.test_mcp_schema_validation import _port
from tests.test_round3_structured_model_input_compiler import _Capability
from tests.test_round6_mcp_production import _generic_tool_stream, _text_stream


def test_dirty_catalog_is_unknown_even_while_connection_is_ready(tmp_path):
    from pulsara_agent.conversation_kernel.context_sources import _render_mcp_catalog
    from pulsara_agent.conversation_kernel.mcp.directory import runtime_rows
    from tests.support.round3 import prepare_test_direct_tool_surface
    from tests.test_mcp_tool_evolution import predecessor

    async def scenario():
        bus = InMemorySubscriptionBus()
        handler = ListenHandler(bus)
        refreshing, release = asyncio.Event(), asyncio.Event()
        revision = 0

        async def tools(_ctx, _params):
            if revision == 2:
                refreshing.set()
                await release.wait()
            return types.ListToolsResult(
                tools=[
                    types.Tool(
                        name="probe",
                        input_schema={
                            "type": "object",
                            "properties": {
                                "value": {
                                    "type": "string" if revision == 0 else "integer"
                                }
                            },
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    )
                ]
            )

        server = Server(
            "dirty-catalog", on_list_tools=tools, on_subscriptions_listen=handler
        )
        async with _sdk_http(server) as endpoint:
            supervisor = _supervisor(tmp_path, endpoint)
            port = _port(tmp_path, supervisor)
            try:
                await supervisor.start()
                port.prepare_tool_surface_safe_point()
                cold = prepare_test_direct_tool_surface(port).capability_exposure_plan
                revision = 1
                await bus.publish(ToolsListChanged())
                async with asyncio.timeout(5):
                    while "catalog" not in supervisor._pending:
                        await asyncio.sleep(0.01)
                port.prepare_tool_surface_safe_point()
                meta = prepare_test_direct_tool_surface(
                    port, predecessor=predecessor(cold)
                ).capability_exposure_plan
                assert meta.mcp_catalog_route_projection.native_meta_identities
                revision = 2
                await bus.publish(ToolsListChanged())
                await asyncio.wait_for(refreshing.wait(), 5)
                unknown = prepare_test_direct_tool_surface(
                    port, predecessor=predecessor(meta)
                ).capability_exposure_plan
                runtime = port._mcp_current
                catalog = runtime.catalog_for_scope(ModelInputScopeKind.ROOT)
                value = json.loads(
                    _render_mcp_catalog(
                        catalog,
                        unknown.mcp_catalog_route_projection,
                        unknown.dispatch_view.planning_input.mcp.source_snapshots,
                    )[0]
                )["servers"][0]
                assert value["status"] == "READY"
                assert value["catalog_complete"] is False
                assert "meta_tool_count" not in value
                assert "resource_count" not in value
                assert (
                    value["unavailable_direct_tools"][0]["reason"]
                    == "SOURCE_UNAVAILABLE"
                )
                _, details, _ = runtime_rows(runtime, unknown, ModelInputScopeKind.ROOT)
                detail = details[("MCP_SERVER", "catalog")]
                assert detail["catalog_discovered"] is False
                assert detail["tool_count"] is None
            finally:
                release.set()
                supervisor.stop_admission()
                closing = asyncio.create_task(supervisor.aclose())
                await port.aclose(timeout_seconds=10)
                await closing

    asyncio.run(scenario())


@pytest.fixture(scope="module")
def evolution_database():
    database = create_migrated_postgres_test_database()
    try:
        yield verified_postgres_provider(database.runtime_dsn)
    finally:
        drop_postgres_test_database(admin_root_dsn(), database.database_name)


def catalogs(request):
    values = []
    for message in request.compiled_input.messages:
        if "pulsara_runtime_observation" in join_text_content(message.content):
            value = decode_runtime_observation(message)
            if value.source_kind is ContextSourceKind.MCP_CATALOG:
                values.append(value)
    return values


def bodies(request):
    return [
        json.loads(join_text_content(message.content))["pulsara_tool_result"]["body"]
        for message in request.compiled_input.messages
        if message.role is MessageRole.TOOL_RESULT
    ]


@pytest.mark.postgres
@pytest.mark.parametrize("wire_api", ["openai_chat_completions", "openai_responses"])
def test_native_evolution_and_meta_refresh_across_real_kernel_turns(
    tmp_path, evolution_database, wire_api
):
    async def scenario():
        repository = ConversationKernelRepository(evolution_database)
        session_id = random_id("session")
        lease = acquire_bound_test_writer(
            repository,
            session_id=session_id,
            workspace_id=random_id("workspace"),
            writer_owner_id=random_id("host"),
            lease_seconds=120,
            deadline_monotonic=monotonic() + 30,
        )
        bus = InMemorySubscriptionBus()
        handler = ListenHandler(bus)
        revision, stage = 0, 0
        physical = []
        retained_ref = None
        output_by_request = {}
        fail_preflight = False

        async def tools(_ctx, _params):
            if revision < 2:
                schema = {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                }
                if revision == 1:
                    schema["properties"]["extra"] = {"type": "boolean"}
            else:
                schema = {
                    "type": "object",
                    "properties": {"number": {"type": "integer"}},
                    "required": ["number"],
                    "additionalProperties": False,
                }
            listed = (
                []
                if revision == 5
                else [
                    types.Tool(
                        name="probe",
                        description=f"Version {revision}",
                        input_schema=schema,
                    )
                ]
            )
            if revision >= 4:
                listed.append(
                    types.Tool(
                        name="new_tool",
                        input_schema={
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    )
                )
            return types.ListToolsResult(tools=listed)

        async def call(_ctx, params):
            physical.append((params.name, dict(params.arguments)))
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=json.dumps(params.arguments))
                ]
            )

        server = Server(
            "evolution",
            on_list_tools=tools,
            on_call_tool=call,
            on_subscriptions_listen=handler,
        )
        async with _sdk_http(server) as endpoint:
            config = _parse_server(
                "catalog",
                {
                    "required": True,
                    "catalog_refresh_interval_ms": "DISABLED",
                    "effect_policy": {"default_effect": "READ_ONLY"},
                    "transport": {
                        "type": "streamable_http",
                        "endpoint": endpoint,
                        "allow_http_localhost": True,
                    },
                },
            )
            supervisor = McpHostSupervisor(
                session_id=session_id,
                workspace_root=tmp_path,
                configs=(config,),
                credential_boundary=ProcessCredentialBoundary(),
            )
            port = _port(tmp_path, supervisor)
            # The shared port fixture has no durable state; bind it to this writer's session.
            port._session_id = session_id
            native = "mcp__catalog__probe"
            new_name = "mcp__catalog__new_tool"

            def latest_ref(request):
                for body in reversed(bodies(request)):
                    try:
                        value = json.JSONDecoder().raw_decode(body)[0]
                    except (ValueError, TypeError):
                        continue
                    if isinstance(value, dict) and value.get("invocation", {}).get("mode") == "META":
                        return value["invocation"]["target"]["tool_ref"]
                raise AssertionError("No FULL inspect result reached the model")

            async def stream(request):
                nonlocal retained_ref
                index = request.model_call_index
                name, args = None, None
                if stage in (0, 1) and index == 1:
                    name, args = native, {"value": "old contract"}
                elif stage == 1 and index == 2:
                    name, args = native, {"value": "old contract", "extra": True}
                elif stage == 2:
                    if index == 1:
                        name, args = native, {"value": "obsolete"}
                    elif index == 2:
                        name, args = (
                            "query_capabilities",
                            {
                                "target": {
                                    "kind": "MCP_TOOL",
                                    "server_id": "catalog",
                                    "tool_name": native,
                                }
                            },
                        )
                    elif index in (3, 4):
                        retained_ref = latest_ref(request)
                        name, args = (
                            "use_new_mcp_tool",
                            {'arguments': {"number": index}, 'target': {"kind":"MCP_TOOL_REF", "tool_ref":retained_ref}},
                        )
                elif stage == 3:
                    if index == 1:
                        name, args = (
                            "use_new_mcp_tool",
                            {'arguments': {"number": 1}, 'target': {"kind":"MCP_TOOL_REF", "tool_ref":retained_ref}},
                        )
                    elif index == 2:
                        name, args = (
                            "query_capabilities",
                            {
                                "target": {
                                    "kind": "MCP_TOOL",
                                    "server_id": "catalog",
                                    "tool_name": native,
                                }
                            },
                        )
                    elif index == 3:
                        name, args = (
                            "use_new_mcp_tool",
                            {'arguments': {"number": 9}, 'target': {"kind":"MCP_TOOL_REF", "tool_ref":latest_ref(request)}},
                        )
                elif stage == 4:
                    if index == 1:
                        name, args = (
                            "query_capabilities",
                            {
                                "target": {
                                    "kind": "MCP_TOOL",
                                    "server_id": "catalog",
                                    "tool_name": new_name,
                                }
                            },
                        )
                    elif index == 2:
                        name, args = (
                            "use_new_mcp_tool",
                            {'arguments': {}, 'target': {"kind":"MCP_TOOL_REF", "tool_ref":latest_ref(request)}},
                        )
                elif stage == 5 and index == 1:
                    name, args = native, {"value": "removed"}
                events = (
                    _text_stream("done")
                    if name is None
                    else _generic_tool_stream(
                        block_id=f"call:{stage}-{index}", tool_name=name, arguments=args
                    )
                )
                output_by_request[id(request)] = (
                    {
                        "type": "message",
                        "id": f"msg_{len(model.requests)}",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "done", "annotations": []}
                        ],
                    }
                    if name is None
                    else {
                        "type": "function_call",
                        "id": f"fc_{len(model.requests)}",
                        "status": "completed",
                        "call_id": f"call:{stage}-{index}",
                        "name": name,
                        "arguments": json.dumps(args),
                    }
                )
                for event in events:
                    yield event

            class Model(CallbackScriptedKernelModel):
                def preflight_execution(self, request, **kwargs):
                    if fail_preflight:
                        assert request.prepared_call.tool_surface.capability_exposure_plan.mcp_catalog_route_projection.native_meta_identities
                        raise RuntimeError("test failure before continuity install")
                    execution = super().preflight_execution(request, **kwargs)
                    take = execution.take_completed_result_once

                    def completed():
                        value = take()
                        if wire_api != "openai_responses":
                            return value
                        payload = freeze_provider_adapter_completed_replay_payload(
                            codec_kind=ProviderAssistantReplayCodecKind.RESPONSES_EXACT_OUTPUT_ITEMS,
                            ordered_items=(
                                freeze_json(output_by_request.pop(id(request))),
                            ),
                        )
                        return replace(
                            value,
                            terminal=replace(
                                value.terminal, completed_replay_payload=payload
                            ),
                            replay_payload=payload,
                        )

                    execution.take_completed_result_once = completed
                    return execution

            model = Model(stream)
            model._model_runtime = model_runtime(model_id="test-pro", wire_api=wire_api)
            model._preparer = DirectKernelModelPort(model_runtime=model._model_runtime)
            bind_test_session(repository, lease, model.model_runtime)
            collector = KernelContextSourceCollector(
                workspace_kind="transient",
                workspace_root=tmp_path,
                capability_composer=_Capability(),
                base_system_prompt="Test MCP evolution",
                display_timezone=timezone.utc,
                mcp_catalog=supervisor,
            )
            runner = ConversationKernelRunner(
                repository=repository,
                writer_lease=lease,
                model=model,
                tools=port,
                live_bus=LiveAgentEventBus(),
                context_source_collector=collector,
                model_resolution_snapshot_provider=model.model_runtime.freeze_resolution_snapshot,
            )
            try:
                await supervisor.start()
                phase_ends = []
                for current_stage in range(6):
                    stage = current_stage
                    if stage:
                        revision = stage
                        await bus.publish(ToolsListChanged())
                        async with asyncio.timeout(10):
                            while "catalog" not in supervisor._pending:
                                await asyncio.sleep(0.01)
                    if stage == 2:
                        scope = ProviderInputContinuityScope(
                            session_id, ModelInputScopeKind.ROOT, None
                        )
                        before = runner._continuity.current_view(scope)
                        fail_preflight = True
                        with pytest.raises(StructuredModelInputCompileError):
                            await runner.run_turn(
                                frozen_test_prompt("candidate that fails installation"),
                                requested_permission_mode=PermissionMode.READ_ONLY,
                            )
                        fail_preflight = False
                        assert runner._continuity.current_view(scope) is before
                        # The physical C runtime was installed, but its epoch plan was not.
                        # Roll back to B before any successful provider-input installation.
                        revision = 0
                        await bus.publish(ToolsListChanged())
                        async with asyncio.timeout(10):
                            while "catalog" not in supervisor._pending:
                                await asyncio.sleep(0.01)
                        stage = -1
                        await runner.run_turn(
                            frozen_test_prompt("rollback before adoption"),
                            requested_permission_mode=PermissionMode.READ_ONLY,
                        )
                        rollback = runner._continuity.current_view(scope)
                        assert not rollback.mcp_route_projection.native_meta_identities
                        assert any(
                            route.version.provider_name == native
                            and route.route.value == "DIRECT"
                            for route in rollback.mcp_route_projection.routes
                        )
                        revision = stage = 2
                        await bus.publish(ToolsListChanged())
                        async with asyncio.timeout(10):
                            while "catalog" not in supervisor._pending:
                                await asyncio.sleep(0.01)
                    result = await runner.run_turn(
                        frozen_test_prompt(f"stage {stage}"),
                        requested_permission_mode=PermissionMode.READ_ONLY,
                    )
                    assert result.final_text == "done"
                    phase_ends.append(model.requests[-1])
                assert physical == [
                    ("probe", {"value": "old contract"}),
                    ("probe", {"value": "old contract"}),
                    ("probe", {"number": 3}),
                    ("probe", {"number": 4}),
                    ("probe", {"number": 9}),
                    ("new_tool", {}),
                ]
                # Native new fields were denied, then incompatible native and stale META
                # refs were denied locally; exactly the six expected calls reached HTTP.
                assert "invalid MCP tool arguments" in "\n".join(bodies(phase_ends[1]))
                assert "use_new_mcp_tool" in "\n".join(bodies(phase_ends[2]))
                assert "MCP_TOOL_REF" in "\n".join(bodies(phase_ends[3]))
                assert len(catalogs(phase_ends[0])) == len(catalogs(phase_ends[1]))
                assert len(catalogs(phase_ends[2])) == len(catalogs(phase_ends[3]))
                assert len(catalogs(phase_ends[4])) > len(catalogs(phase_ends[3]))
                for before, after in zip(model.requests, model.requests[1:]):
                    old, new = (
                        before.wire_input_plan.materialization,
                        after.wire_input_plan.materialization,
                    )
                    assert canonical_json_bytes(
                        old.root_policy_value
                    ) == canonical_json_bytes(new.root_policy_value)
                    assert tuple(map(canonical_json_bytes, old.tool_items)) == tuple(
                        map(canonical_json_bytes, new.tool_items)
                    )
                    assert tuple(
                        map(
                            canonical_json_bytes,
                            new.ordered_input_items[: len(old.ordered_input_items)],
                        )
                    ) == tuple(map(canonical_json_bytes, old.ordered_input_items))
            finally:
                supervisor.stop_admission()
                closing = asyncio.create_task(supervisor.aclose())
                await port.aclose(timeout_seconds=10)
                await closing

    asyncio.run(scenario())
