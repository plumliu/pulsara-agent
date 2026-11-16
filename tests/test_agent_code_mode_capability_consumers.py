"""Observable consumer, scope and current-Work contracts for the hard cut."""
from tests.support.mcp import finish_connection_attempts

from tests.support.model_config import scheduled_task_service
from dataclasses import replace

import pytest

from pulsara_agent.capability.source_query import parse_query
from pulsara_agent.capability.contracts import (
    CapabilityKind,
    CapabilitySourceKind,
    capability_identity,
    capability_source_ref,
)

from pulsara_agent.conversation_kernel.mcp.meta import (
    ProcessLocalNewMcpToolRefOwner,
)
from pulsara_agent.hooks.config_parser import parse_hook_config
from pulsara_agent.hooks.contracts import HookScopeKind, HookScopePolicy, HookEventType
from pulsara_agent.mcp_config import McpScopePolicy
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.plugins.mcp_connection import (
    PluginMcpConnectionOverlay,
    overlay_from_dict,
    overlay_to_dict,
)
from tests.test_round9_2_hook_subsystem import _provenance, _view

IDENTITY = capability_identity(
    kind=CapabilityKind.TOOL,
    source=capability_source_ref(CapabilitySourceKind.MCP_SERVER, "fixture"),
    stable_name="tool",
)


def test_accepted_ref_is_session_scoped_and_reused_without_a_delivery_gate():
    from pulsara_agent.identity import random_id

    owner = ProcessLocalNewMcpToolRefOwner()
    scope = dict(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        session_id=random_id("session"),
    )
    facts = dict(
        capability_identity=IDENTITY, binding=("tool", "actual schema", "actual policy")
    )
    native = owner.prepare(**scope, **facts, result_entry_id=random_id("entry"))
    cell = owner.prepare(**scope, **facts, result_entry_id=random_id("entry"))
    assert native.ref is cell.ref
    with pytest.raises(LookupError):
        owner.resolve_callable(native.ref.opaque_token, **scope)
    owner.settle(prepared=native, committed=True)
    assert owner.resolve_callable(cell.ref.opaque_token, **scope) is cell.ref
    owner.settle(prepared=cell, committed=False)
    assert owner.resolve_callable(native.ref.opaque_token, **scope) is native.ref
    with pytest.raises(LookupError):
        owner.resolve_callable(
            native.ref.opaque_token, **{**scope, "session_id": random_id("session")}
        )


def test_repeated_accepted_queries_reuse_binding_and_reconcile_actual_changes():
    from pulsara_agent.identity import random_id

    owner = ProcessLocalNewMcpToolRefOwner()
    scope = dict(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        session_id=random_id("session"),
    )
    tokens = set()
    for _ in range(1100):
        item = owner.prepare(
            **scope,
            capability_identity=IDENTITY,
            binding=("schema", "policy"),
            result_entry_id=random_id("entry"),
        )
        owner.settle(prepared=item, committed=True)
        tokens.add(item.ref.opaque_token)
    assert len(tokens) == 1
    owner.reconcile(
        **scope,
        server_ids={"fixture"},
        complete_server_ids={"fixture"},
        bindings={IDENTITY: ("changed schema", "policy")},
    )
    with pytest.raises(LookupError):
        owner.resolve_callable(item.ref.opaque_token, **scope)
    current = owner.prepare(
        **scope,
        capability_identity=IDENTITY,
        binding=("changed schema", "policy"),
        result_entry_id=random_id("entry"),
    )
    owner.settle(prepared=current, committed=True)
    assert current.ref.opaque_token not in tokens
    assert owner.resolve_callable(current.ref.opaque_token, **scope) is current.ref
    owner.close()
    with pytest.raises(LookupError):
        owner.resolve_callable(current.ref.opaque_token, **scope)


def test_hook_scope_selection_is_shared_by_plugin_and_local_sources(tmp_path):
    parsed = parse_hook_config(
        b'{"hooks":{"PreToolUse":[{"hooks":['
        b'{"type":"command","command":"root","scope_policy":"ROOT"},'
        b'{"type":"command","command":"child","scope_policy":"SUBAGENT"},'
        b'{"type":"command","command":"both","scope_policy":"BOTH"}]}]}}',
        provenance=_provenance(tmp_path),
    )
    assert not parsed.diagnostics
    view = _view(tmp_path, parsed.definitions)
    assert [
        item.command
        for _, item in view.selected_definitions(
            HookEventType.PRE_TOOL_USE_EVENT, HookScopeKind.ROOT
        )
    ] == ["root", "both"]
    assert [
        item.command
        for _, item in view.selected_definitions(
            HookEventType.PRE_TOOL_USE_EVENT, HookScopeKind.SUBAGENT
        )
    ] == ["child", "both"]
    assert parsed.definitions[2].scope_policy is HookScopePolicy.BOTH


def test_plugin_and_manual_mcp_share_typed_scope_policy():
    overlay = PluginMcpConnectionOverlay(
        "docs", "stdio", scope_policy=McpScopePolicy.ROOT_AND_SUBAGENTS
    )
    assert overlay_from_dict(overlay_to_dict(overlay)) == overlay
    assert overlay.validation_entry()["scope_policy"] == "ROOT_AND_SUBAGENTS"
    assert (
        PluginMcpConnectionOverlay("docs", "stdio").scope_policy
        is McpScopePolicy.ROOT_AND_SUBAGENTS
    )


def test_subagent_queries_its_mcp_scope_and_cannot_discover_root_skill_or_hook_sources(tmp_path):
    from pulsara_agent.capability.source_query import CapabilityQueryError
    assert parse_query({'kind':'MCP'},subagent=True)=={'kind':'MCP'}
    for kind in ('HOOK','SKILL','PLUGIN'):
        with pytest.raises(CapabilityQueryError,match='子代理'):
            parse_query({'kind':kind},subagent=True)


def test_code_mode_uses_catalog_profile_free_run_agent_schema(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from jsonschema import Draft202012Validator
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_policy import (
        DefaultToolDispatchAuthorizationPolicy,
    )
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from pulsara_agent.primitives.context import thaw_json
    from pulsara_agent.primitives.permission import PermissionMode
    from tests.support.round3 import prepare_test_direct_tool_surface

    async def run():
        port = DirectKernelToolPort(
            scheduled_tasks=scheduled_task_service(),
            workspace_root=tmp_path,
            host_owner_id="schema-host",
            session_id="session:root",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port.bind_subagent_port(SimpleNamespace(tool_names=frozenset({"run_agent"})))
        try:
            surface = prepare_test_direct_tool_surface(
                port, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS
            )
            from pulsara_agent.capability.builtin_catalog import execution_catalog_entry
            from pulsara_agent.ports.tool_execution import thaw_tool_json_object

            assert "run_agent" not in {
                spec.name for spec in surface.model_surface.tool_specs
            }
            code = next(
                spec
                for spec in port.freeze_code_mode_tool_directory(
                    surface, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS
                ).tool_specs
                if spec.name == "run_agent"
            )
            assert thaw_json(code.parameters) == thaw_tool_json_object(
                execution_catalog_entry("run_agent").descriptor.input_schema
            )
            schema = thaw_json(code.parameters)
            assert "profile" not in schema["properties"]
            for value in ("research_worker", None):
                assert not Draft202012Validator(schema).is_valid(
                    {"instruction": "work", "profile": value}
                )
        finally:
            await port.aclose()

    asyncio.run(run())


def test_native_mcp_query_is_callable_from_cells_and_changed_binding_does_not_replay_accepted_effect(
    tmp_path,
):
    import asyncio
    import json
    from mcp.server.lowlevel import Server
    from mcp.server.subscriptions import (
        InMemorySubscriptionBus,
        ListenHandler,
        ToolsListChanged,
    )
    import mcp_types as types
    from tests.test_mcp_catalog_subscriptions import _sdk_http
    from tests.test_mcp_meta_only_integration import invoke
    from tests.support.round3 import (
        prepare_test_direct_tool_surface,
        _UnavailableTestDecisionPort,
    )
    from pulsara_agent.conversation_kernel.mcp.supervisor import (
        McpHostSupervisor,
        McpSnapshotStale,
    )
    from pulsara_agent.conversation_kernel.tool_runtime import (
        DirectKernelToolPort,
        _await_mcp_operation,
    )
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_policy import (
        DefaultToolDispatchAuthorizationPolicy,
    )
    from pulsara_agent.mcp_config import _parse_server
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
    from pulsara_agent.identity import random_id
    from pulsara_agent.primitives.permission import PermissionMode

    async def scenario():
        revision = 1
        bus = InMemorySubscriptionBus()
        received = []
        slow_started, slow_release = asyncio.Event(), asyncio.Event()

        async def tools(_ctx, _params):
            return types.ListToolsResult(
                tools=[
                    types.Tool(
                        name="probe",
                        input_schema={
                            "type": "object",
                            "properties": {
                                "value": {
                                    "type": "string" if revision == 1 else "integer"
                                }
                            },
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    )
                ]
            )

        async def call(_ctx, params):
            received.append(params.arguments)
            if params.arguments.get("value") == "slow":
                slow_started.set()
                await slow_release.wait()
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=json.dumps(params.arguments))
                ]
            )

        server = Server(
            "shared-ref",
            on_list_tools=tools,
            on_call_tool=call,
            on_subscriptions_listen=ListenHandler(bus),
        )
        async with _sdk_http(server) as endpoint:
            supervisor = McpHostSupervisor(
                workspace_root=tmp_path,
                configs=(
                    _parse_server(
                        "catalog",
                        {
                            "catalog_refresh_interval_ms": "DISABLED",
                            "effect_policy": {"default_effect": "READ_ONLY"},
                            "transport": {
                                "type": "streamable_http",
                                "endpoint": endpoint,
                                "allow_http_localhost": True,
                            },
                        },
                    ),
                ),
                credential_boundary=ProcessCredentialBoundary(),
            )
            port = DirectKernelToolPort(
                scheduled_tasks=scheduled_task_service(),
                workspace_root=tmp_path,
                host_owner_id=random_id("host"),
                session_id=random_id("session"),
                live_bus=LiveAgentEventBus(),
                authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            )
            port.bind_mcp_supervisor(supervisor)
            port.bind_interaction_port(object())
            port.bind_subagent_port(type("EmptySubagent", (), {"tool_names": ()})())
            port.bind_memory_port(type("EmptyMemory", (), {"tool_names": ()})())
            port.bind_decision_port(_UnavailableTestDecisionPort())
            port.bind_capability_reload_port(object())
            port.seal_builtin_composition()
            borrow = slow = None
            try:
                await supervisor.start()
                await finish_connection_attempts(supervisor)
                port.prepare_tool_surface_safe_point()
                detail = await invoke(
                    port,
                    "query_capabilities",
                    {
                        "kind": "MCP",
                        "target": {
                            "kind": "MCP_TOOL",
                            "server_id": "catalog",
                            "remote_tool_name": "probe",
                        },
                    },
                )
                record = detail["content"][0]["value"]
                target = record["invocation"]["target"]
                surface = prepare_test_direct_tool_surface(
                    port, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS
                )
                directory = port.freeze_code_mode_tool_directory(
                    surface, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS
                )
                for value in ("one", "two"):
                    result = await invoke(
                        port,
                        "call_mcp_tool",
                        {"target": target, "arguments": {"value": value}},
                        cell=random_id("cell"),
                        directory=directory,
                    )
                    assert result["meta"]["status"] == "SUCCESS"
                    assert json.loads(result["content"][0]["value"]) == {"value": value}
                borrow = port.borrow_code_mode_surface(
                    directory, cell_id=random_id("cell")
                )
                scope = port._mcp_ref_consumer_for_borrow(borrow)
                ref = port._mcp_meta_refs.resolve_callable(target["tool_ref"], **scope)
                executor, _ = port._resolve_meta_ref_executor(
                    ref=ref, surface_borrow=borrow
                )
                permit = executor.admit(
                    session_id=port._session_id,
                    scope_kind=ModelInputScopeKind.ROOT,
                    turn_id=random_id("turn"),
                    tool_call_id=random_id("call"),
                )
                permit.mark_attempt_accepted()
                cancelled = asyncio.Event()
                operation = asyncio.create_task(
                    executor.invoke(
                        permit, {"value": "slow"}, cell_cancel_event=cancelled
                    )
                )
                slow = asyncio.create_task(
                    _await_mcp_operation(operation, permit=permit)
                )
                await asyncio.wait_for(slow_started.wait(), 5)
                cancelled.set()
                slow.cancel()
                await asyncio.sleep(0)
                assert not slow.done()
                revision = 2
                await bus.publish(ToolsListChanged())
                async with asyncio.timeout(5):
                    while (
                        executor.lease._slot.dispatch_fence_state.value
                        != "DIRTY_FENCED"
                    ):
                        await asyncio.sleep(0.01)
                with pytest.raises(McpSnapshotStale):
                    executor.admit(
                        session_id=port._session_id,
                        scope_kind=ModelInputScopeKind.ROOT,
                        turn_id=random_id("turn"),
                        tool_call_id=random_id("call"),
                    )
                slow_release.set()
                known, caller_cancelled = await asyncio.wait_for(slow, 5)
                assert known.state == "SUCCESS" and caller_cancelled
                assert permit.state.value == "RELEASED"
                async with asyncio.timeout(5):
                    while "catalog" not in supervisor._pending:
                        await asyncio.sleep(0.01)
                port.prepare_tool_surface_safe_point()
                with pytest.raises(LookupError):
                    port._resolve_meta_ref_executor(ref=ref, surface_borrow=borrow)
                updated = await invoke(
                    port,
                    "query_capabilities",
                    {
                        "kind": "MCP",
                        "target": {
                            "kind": "MCP_TOOL",
                            "server_id": "catalog",
                            "remote_tool_name": "probe",
                        },
                    },
                )
                current = updated["content"][0]["value"]
                assert (
                    current["input_schema"]["properties"]["value"]["type"] == "integer"
                )
                assert current["invocation"]["target"]["tool_ref"] != target["tool_ref"]
                assert received == [
                    {"value": "one"},
                    {"value": "two"},
                    {"value": "slow"},
                ]
            finally:
                slow_release.set()
                if slow is not None:
                    await asyncio.wait_for(slow, 5)
                if borrow is not None:
                    borrow.close()
                supervisor.stop_admission()
                closing = asyncio.create_task(supervisor.aclose())
                await port.aclose(timeout_seconds=10)
                await closing

    asyncio.run(scenario())


def test_pretool_hook_deny_wins_and_preserves_ordered_additional_context(tmp_path):
    import json
    from pulsara_agent.hooks.contracts import GateDecision
    from pulsara_agent.hooks.dispatcher import _aggregate
    from pulsara_agent.hooks.output_parser import (
        parse_handler_output,
        ValidHandlerContribution,
    )
    from tests.test_round9_2_hook_subsystem import _definition, _execution

    definition = _definition(tmp_path, HookEventType.PRE_TOOL_USE_EVENT, "review")

    def parse(value):
        execution = _execution(
            definition,
            stdout=json.dumps(
                {"hookSpecificOutput": {"hookEventName": "PreToolUse", **value}}
            ).encode(),
        )
        contribution = parse_handler_output(execution)
        assert isinstance(contribution, ValidHandlerContribution)
        return replace(contribution, scrub_set=execution.scrub_set)

    first = parse({"additionalContext": "Read the project instructions."})
    second = parse({"additionalContext": "Check the execution results."})
    assert isinstance(first, ValidHandlerContribution)
    proceeding = _aggregate(
        HookEventType.PRE_TOOL_USE_EVENT,
        (first, second),
        continuation_already_used=False,
    )
    assert proceeding.decision is GateDecision.PROCEED
    assert [entry.text for entry in proceeding.context_entries] == [
        "Read the project instructions.",
        "Check the execution results.",
    ]
    denied = parse(
        {
            "permissionDecision": "deny",
            "permissionDecisionReason": "Execution is blocked.",
        }
    )
    outcome = _aggregate(
        HookEventType.PRE_TOOL_USE_EVENT,
        (first, denied, second),
        continuation_already_used=False,
    )
    assert outcome.decision is GateDecision.BLOCK
    assert outcome.reason == "Execution is blocked."
    assert outcome.context_entries == proceeding.context_entries


def test_shared_hook_dispatch_exposes_real_subagent_session_and_instruction_origin(
    tmp_path,
):
    import asyncio
    from pulsara_agent.hooks.contracts import (
        HookDispatchEnvelope,
        QueuedPromptRef,
        UserPromptSubmitInput,
        HookDispatchScopeRef,
    )
    from pulsara_agent.hooks.dispatcher import KernelHookDispatcher
    from pulsara_agent.hooks.matcher import event_matcher_subject
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
    from tests.test_round9_2_hook_subsystem import _definition, _ScriptedExecutor
    from time import monotonic

    async def scenario():
        definition = replace(
            _definition(tmp_path, HookEventType.USER_PROMPT_SUBMIT_EVENT, "child"),
            scope_policy=HookScopePolicy.SUBAGENT,
        )
        executor = _ScriptedExecutor({"child": (0, b"work context", b"")})
        dispatcher = KernelHookDispatcher(
            initial_view=_view(tmp_path, (definition,)),
            workspace_root=tmp_path,
            credential_boundary=ProcessCredentialBoundary(),
            executor=executor,
        )
        try:
            scope = HookDispatchScopeRef(
                object(), object(), HookScopeKind.SUBAGENT, "session:actual-child"
            )
            public = UserPromptSubmitInput(
                session_id="session:actual-child",
                cwd=str(tmp_path),
                model="model:test",
                session_kind="SUBAGENT",
                input_origin="AGENT_INSTRUCTION",
                turn_id="turn:actual-work",
                prompt="actual instruction",
                permission_mode="bypassPermissions",
            )
            outcome = await dispatcher.dispatch(
                HookDispatchEnvelope(
                    dispatcher.capture_view(),
                    scope,
                    public,
                    QueuedPromptRef(
                        None,
                        "queue:actual-work",
                        "turn:actual-work",
                        "entry:instruction",
                        "revision:work",
                    ),
                    monotonic() + 10,
                ),
                matcher_subject=event_matcher_subject(
                    HookEventType.USER_PROMPT_SUBMIT_EVENT
                ),
            )
            assert outcome.context_entries
            assert len(executor.calls) == 1
            sent = executor.calls[0].public_stdin
            assert sent["session_id"] == "session:actual-child"
            assert sent["session_kind"] == "SUBAGENT"
            assert sent["input_origin"] == "AGENT_INSTRUCTION"
        finally:
            await dispatcher.aclose(deadline_monotonic=monotonic() + 5)

    asyncio.run(scenario())


def test_kernel_tool_result_remote_identity_closed_branches():
    from pulsara_agent.conversation_kernel.tool_contracts import KernelToolResult
    from pulsara_agent.identity import derive_id
    from pulsara_agent.ports.tool_execution import ToolBusinessValue

    process = derive_id("process", "result-test", "one")
    remote = derive_id("mcp-tool", "remote-tool", "fixture", "1", "tool")
    verification = "sha256:" + "a" * 64
    KernelToolResult(
        state="SUCCESS", content=ToolBusinessValue(), remote_identity=process
    )
    KernelToolResult(
        state="SUCCESS",
        content=ToolBusinessValue(),
        remote_identity=remote,
        remote_identity_verification=verification,
    )
    KernelToolResult(state="SUCCESS", content=ToolBusinessValue())
    for identity, verifier in (
        (None, verification),
        (process, verification),
        (remote, None),
        (remote, "sha256:short"),
    ):
        with pytest.raises(ValueError, match="remote identity"):
            KernelToolResult(
                state="SUCCESS",
                content=ToolBusinessValue(),
                remote_identity=identity,
                remote_identity_verification=verifier,
            )
