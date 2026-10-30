"""Observable consumer, scope and current-Work contracts for the hard cut."""
from dataclasses import replace

import pytest

from pulsara_agent.capability.source_query import parse_query
from pulsara_agent.conversation_kernel.mcp.meta import (
    McpToolRefConsumerKind,
    ProcessLocalNewMcpToolRefOwner,
)
from pulsara_agent.hooks.config_parser import parse_hook_config
from pulsara_agent.hooks.contracts import HookScopeKind, HookScopePolicy, HookEventType
from pulsara_agent.mcp_config import McpScopePolicy
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.plugins.mcp_connection import PluginMcpConnectionOverlay, overlay_from_dict, overlay_to_dict
from tests.test_round9_2_hook_subsystem import _provenance, _view


def test_full_inspect_delivery_is_consumer_and_session_specific_and_cell_survives_compaction():
    owner = ProcessLocalNewMcpToolRefOwner()
    common = dict(conversation_scope_kind=ModelInputScopeKind.ROOT,
                  session_id="session:root", continuity_epoch_nonce="epoch:one")
    facts = dict(capability_identity_fingerprint="identity", tool_semantic_fingerprint="schema",
                 mcp_execution_policy_fingerprint="policy", tool_route_fingerprint="route",
                 runtime_generation=3)
    model = owner.prepare(**common, **facts, result_entry_id="entry:model")
    cell_scope = {**common, "continuity_epoch_nonce": "cell:one",
                  "consumer_kind": McpToolRefConsumerKind.CODE_CELL, "consumer_id": "cell:one"}
    cell = owner.prepare(**cell_scope, **facts, result_entry_id="entry:cell")
    assert cell.ref.opaque_token != model.ref.opaque_token
    for prepared in (model, cell):
        owner.settle(prepared=prepared, committed=True)
    with pytest.raises(LookupError, match="DORMANT"):
        owner.resolve_callable(cell.ref.opaque_token, **cell_scope)
    assert not owner.install_full_result(**common, result_entry_id="entry:cell")
    assert owner.install_full_result(**cell_scope, result_entry_id="entry:cell")
    with pytest.raises(LookupError):
        owner.resolve_callable(cell.ref.opaque_token, **{**cell_scope, "session_id": "session:other"})
    with pytest.raises(LookupError):
        owner.resolve_callable(cell.ref.opaque_token, **{**cell_scope, "consumer_id": "cell:other"})
    owner.retire_scope_except_epoch(conversation_scope_kind=ModelInputScopeKind.ROOT,
                                   session_id="session:root", continuity_epoch_nonce="epoch:two")
    assert owner.resolve_callable(cell.ref.opaque_token, **cell_scope) is cell.ref
    owner.retire_consumer(consumer_kind=McpToolRefConsumerKind.CODE_CELL, consumer_id="cell:one")
    with pytest.raises(LookupError, match="NOT_FOUND"):
        owner.resolve_callable(cell.ref.opaque_token, **cell_scope)



def test_repeated_code_inspect_reuses_current_ref_and_prunes_obsolete_facts():
    owner = ProcessLocalNewMcpToolRefOwner()
    scope = dict(conversation_scope_kind=ModelInputScopeKind.ROOT, session_id="session:root",
                 continuity_epoch_nonce="cell:long", consumer_kind=McpToolRefConsumerKind.CODE_CELL,
                 consumer_id="cell:long")
    facts = dict(capability_identity_fingerprint="identity", tool_semantic_fingerprint="schema",
                 mcp_execution_policy_fingerprint="policy", tool_route_fingerprint="CODE_CELL", runtime_generation=1)
    tokens = set()
    for i in range(1100):  # More than the existing MODEL bound is not a cell lifetime cap.
        item = owner.prepare(**scope, **facts, result_entry_id=f"entry:{i}")
        owner.settle(prepared=item, committed=True)
        owner.install_full_result(**scope, result_entry_id=f"entry:{i}")
        tokens.add(item.ref.opaque_token)
    assert len(tokens) == 1
    ref = owner.resolve_callable(item.ref.opaque_token, **scope)
    assert not item.record.committed_result_entry_ids
    owner.retire_unroutable(**scope, allowed={("identity", "new-schema", "policy", "CODE_CELL", 2)})
    with pytest.raises(LookupError):
        owner.resolve_callable(ref.opaque_token, **scope)
    current = owner.prepare(**scope, **{**facts, "tool_semantic_fingerprint": "new-schema", "runtime_generation": 2},
                            result_entry_id="entry:new")
    assert current.ref.opaque_token != ref.opaque_token
    owner.retire_consumer(consumer_kind=McpToolRefConsumerKind.CODE_CELL, consumer_id="cell:long")
    assert not owner._refs and not owner._prepared

def test_hook_scope_selection_is_shared_by_plugin_and_local_sources(tmp_path):
    parsed = parse_hook_config(b'{"hooks":{"PreToolUse":[{"hooks":['
        b'{"type":"command","command":"root","scope_policy":"ROOT"},'
        b'{"type":"command","command":"child","scope_policy":"SUBAGENT"},'
        b'{"type":"command","command":"both","scope_policy":"BOTH"}]}]}}',
        provenance=_provenance(tmp_path))
    assert not parsed.diagnostics
    view = _view(tmp_path, parsed.definitions)
    assert [item.command for _, item in view.selected_definitions(HookEventType.PRE_TOOL_USE_EVENT, HookScopeKind.ROOT)] == ["root", "both"]
    assert [item.command for _, item in view.selected_definitions(HookEventType.PRE_TOOL_USE_EVENT, HookScopeKind.SUBAGENT)] == ["child", "both"]
    assert parsed.definitions[2].scope_policy is HookScopePolicy.BOTH


def test_plugin_and_manual_mcp_share_typed_scope_policy():
    overlay = PluginMcpConnectionOverlay("docs", "stdio", scope_policy=McpScopePolicy.ROOT_AND_SUBAGENTS)
    assert overlay_from_dict(overlay_to_dict(overlay)) == overlay
    assert overlay.validation_entry()["scope_policy"] == "ROOT_AND_SUBAGENTS"
    assert PluginMcpConnectionOverlay("docs", "stdio").scope_policy is McpScopePolicy.ROOT_ONLY


def test_subagent_can_query_shared_sources_without_management_authority(tmp_path):
    assert parse_query({"kind": "HOOK_SOURCE", "scope": "USER"}, subagent=True)["kind"] == "HOOK_SOURCE"
    assert parse_query({"kind": "SKILL"}, subagent=True)["kind"] == "SKILL"


def test_native_and_code_mode_expose_same_profile_free_run_agent_schema(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from jsonschema import Draft202012Validator
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
    from pulsara_agent.conversation_kernel.tool_surface import CodeModeToolDirectory
    from pulsara_agent.primitives.context import thaw_json
    from pulsara_agent.primitives.permission import PermissionMode
    from tests.support.round3 import prepare_test_direct_tool_surface

    async def run():
        port = DirectKernelToolPort(workspace_root=tmp_path, host_owner_id="schema-host",
            session_id="session:root", live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        port.bind_subagent_port(SimpleNamespace(tool_names=frozenset({"run_agent"})))
        try:
            surface = prepare_test_direct_tool_surface(port, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            native = next(spec for spec in surface.model_surface.tool_specs if spec.name == "run_agent")
            code = next(spec for spec in CodeModeToolDirectory.freeze(surface).tool_specs if spec.name == "run_agent")
            assert native.parameters == code.parameters
            schema = thaw_json(code.parameters)
            assert "profile" not in schema["properties"]
            for value in ("research_worker", None):
                assert not Draft202012Validator(schema).is_valid({"instruction": "work", "profile": value})
        finally:
            await port.aclose()
    asyncio.run(run())


def test_code_cell_inspect_executes_actual_mcp_without_provider_install_and_rejects_changed_binding(tmp_path):
    import asyncio
    import json
    from types import SimpleNamespace
    from mcp.server.lowlevel import Server
    from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ToolsListChanged
    import mcp_types as types
    from tests.test_mcp_catalog_subscriptions import _sdk_http
    from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor, McpSnapshotStale
    from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort, _await_mcp_operation
    from pulsara_agent.mcp_config import _parse_server
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary

    async def scenario():
        revision = 1
        bus = InMemorySubscriptionBus()
        received = []
        slow_started, slow_release = asyncio.Event(), asyncio.Event()
        slow = None

        async def tools(_ctx, _params):
            return types.ListToolsResult(tools=[types.Tool(name="probe", input_schema={
                "type": "object", "properties": {"value": {"type": "string" if revision == 1 else "integer"}},
                "required": ["value"], "additionalProperties": False})])

        async def call(_ctx, params):
            received.append(params.arguments)
            if params.arguments.get("value") == "slow":
                slow_started.set()
                await slow_release.wait()
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(params.arguments))])

        server = Server("code-consumer", on_list_tools=tools, on_call_tool=call,
                        on_subscriptions_listen=ListenHandler(bus))
        async with _sdk_http(server) as endpoint:
            supervisor = McpHostSupervisor(session_id="session:root", workspace_root=tmp_path,
                configs=(_parse_server("catalog", {"required": True, "catalog_refresh_interval_ms": "DISABLED",
                    "effect_policy": {"default_effect": "READ_ONLY"}, "transport": {"type": "streamable_http",
                    "endpoint": endpoint, "allow_http_localhost": True}}),),
                credential_boundary=ProcessCredentialBoundary())
            first = second = None
            try:
                await supervisor.start()
                first = supervisor.install_pending_at_safe_point()
                # The actual Host MCP runtime is the authority; this cell has no provider epoch.
                port = object.__new__(DirectKernelToolPort)
                port._surface_generation = 1
                port._mcp_runtime_by_surface_generation = {1: first}
                port._mcp_meta_refs = ProcessLocalNewMcpToolRefOwner()
                port._mcp_ref_settlements = {}
                borrow = SimpleNamespace(code_cell_id="cell:actual", tool_specs=(), prepared=SimpleNamespace(
                    access=SimpleNamespace(surface_generation=1, conversation_scope_kind=ModelInputScopeKind.ROOT,
                                           session_id="session:root"),
                    model_surface=SimpleNamespace(tool_specs=())))
                context = SimpleNamespace(surface_borrow=borrow, result_entry_id="entry:first")
                target = {"kind": "MCP_TOOL", "server_id": "catalog", "tool_name": "mcp__catalog__probe"}
                result = port._inspect_mcp_tool_result(target=target, invocation_context=context)
                body = json.loads(result.content)
                assert body["invocation"]["mode"] == "META"
                token = body["invocation"]["target"]["tool_ref"]
                prepared = result.process_local_settlement.prepared
                port._mcp_meta_refs.settle(prepared=prepared, committed=True)
                scope = port._mcp_ref_consumer_for_borrow(borrow)
                with pytest.raises(LookupError, match="DORMANT"):
                    port._mcp_meta_refs.resolve_callable(token, **scope)
                assert port.install_code_cell_tool_result_delivery(result_entry_id="entry:first", cell_id="cell:actual", borrow=borrow)
                ref = port._mcp_meta_refs.resolve_callable(token, **scope)
                executor, generation = port._resolve_meta_ref_executor(ref=ref, surface_borrow=borrow)
                assert generation == 1
                permit = executor.admit(session_id="session:root", scope_kind=ModelInputScopeKind.ROOT,
                                        turn_id="turn:actual", tool_call_id="tool:actual")
                permit.mark_attempt_accepted()
                known = await executor.invoke(permit, {"value": "actual"}, cell_cancel_event=None)
                assert known.state == "SUCCESS" and received == [{"value": "actual"}]
                port._mcp_meta_refs.retire_scope_except_epoch(conversation_scope_kind=ModelInputScopeKind.ROOT,
                    session_id="session:root", continuity_epoch_nonce="epoch:compacted")
                assert port._mcp_meta_refs.resolve_callable(token, **scope) is ref
                slow_permit = executor.admit(session_id="session:root", scope_kind=ModelInputScopeKind.ROOT,
                    turn_id="turn:actual", tool_call_id="tool:slow")
                slow_permit.mark_attempt_accepted()
                cell_stopped = asyncio.Event()
                operation = asyncio.create_task(executor.invoke(slow_permit, {"value": "slow"}, cell_cancel_event=cell_stopped))
                slow = asyncio.create_task(_await_mcp_operation(operation, permit=slow_permit))
                await asyncio.wait_for(slow_started.wait(), 5)
                cell_stopped.set()
                slow.cancel()
                await asyncio.sleep(0)
                assert not slow.done()
                revision = 2
                await bus.publish(ToolsListChanged())
                async with asyncio.timeout(5):
                    while executor.lease._slot.dispatch_fence_state.value != "DIRTY_FENCED":
                        await asyncio.sleep(.01)
                with pytest.raises(McpSnapshotStale):
                    executor.admit(session_id="session:root", scope_kind=ModelInputScopeKind.ROOT,
                                   turn_id="turn:actual", tool_call_id="tool:blocked-after-refresh")
                # SDK catalog refresh drains accepted calls before changing its schema cache.
                slow_release.set()
                known, caller_cancelled = await asyncio.wait_for(slow, 5)
                assert known.state == "SUCCESS" and caller_cancelled
                assert slow_permit.state.value == "RELEASED"
                async with asyncio.timeout(5):
                    while "catalog" not in supervisor._pending:
                        await asyncio.sleep(.01)
                second = supervisor.install_pending_at_safe_point()
                port._surface_generation = 2
                port._mcp_runtime_by_surface_generation[2] = second
                with pytest.raises(LookupError, match="BINDING_STALE"):
                    port._resolve_meta_ref_executor(ref=ref, surface_borrow=borrow)
                context.result_entry_id = "entry:second"
                current = port._inspect_mcp_tool_result(target=target, invocation_context=context)
                updated = json.loads(current.content)
                assert updated["input_schema"]["properties"]["value"]["type"] == "integer"
                assert updated["invocation"]["target"]["tool_ref"] != token
                port.release_code_cell_mcp_refs("cell:actual")
                with pytest.raises(LookupError):
                    port._mcp_meta_refs.resolve_callable(token, **scope)
            finally:
                slow_release.set()
                if slow is not None:
                    await asyncio.wait_for(slow, 5)
                if first is not None:
                    first.release()
                if second is not None:
                    second.release()
                await supervisor.aclose()

    asyncio.run(scenario())


def test_monitor_current_work_delivery_filter_compaction_and_terminal_cleanup_share_manager(tmp_path):
    import os
    import sys
    from time import monotonic
    from pulsara_agent.identity import random_id
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.ports.terminal_observation import ExistingTurnInstallation, NewTurnInstallation
    from pulsara_agent.terminal_process.manager import ProcessRegistry
    from pulsara_agent.terminal_process.monitor import TerminalMonitorCoordinator, TerminalMonitorPolicy

    owner = random_id("host")
    root, agent = random_id("session"), random_id("session")
    turn = random_id("turn")
    registry = ProcessRegistry()
    registry.activate_owner(owner)
    state, yielded = registry.exec_with_yield(command="background", cwd=tmp_path, yield_time_ms=0,
        tty=False, max_lifetime_seconds=None, owner_host_session_id=owner,
        shell_argv=(sys.executable, "-c", "import time;time.sleep(30)"),
        decision_deadline_monotonic=monotonic()+5, env=dict(os.environ))
    assert yielded
    root_bus, child_bus = LiveAgentEventBus(), LiveAgentEventBus()
    root_observer, _, root_revision = root_bus.subscribe()
    child_observer, _, child_revision = child_bus.subscribe()
    coordinator = TerminalMonitorCoordinator(session_id=root, owner_epoch=owner, registry=registry,
        live_bus=root_bus, wake_scheduler=lambda: None)
    coordinator.bind_live_bus(agent, child_bus)
    try:
        registration = coordinator.prepare_registration(process_id=state.process_id, origin_turn_id=turn,
            origin_attempt_id=random_id("attempt"), origin_result_entry_id=random_id("entry"),
            writer_generation=1, authorization_reference="policy:actual", recipient_session_id=agent,
            registering_agent_id=agent, work_turn_id=turn,
            policy=TerminalMonitorPolicy(min_new_output_chars=1, quiet_period_ms=0))
        coordinator.settle_registration(registration, committed=True)
        assert coordinator.registration_scope(registration.monitor_id) == (agent, agent, turn)
        assert len(coordinator.list_current()) == 1  # ROOT management is Host-wide.
        assert len(coordinator.list_current(agent_id=agent, work_turn_id=turn)) == 1
        assert coordinator.list_current(agent_id=agent, work_turn_id=random_id("turn")) == ()
        assert coordinator.cancel(registration.monitor_id, agent_id=root, work_turn_id=turn) == "not_found"
        assert len(coordinator.freeze_compaction_handoff(agent_id=agent, work_turn_id=turn)) == 1
        state.output.append_raw(b"work observation")
        coordinator._evaluate(registration.monitor_id, monotonic()+6)
        fields = dict(monitor_id=registration.monitor_id, workspace_id=random_id("workspace"),
                      writer_generation=1, actor_id=owner)
        assert coordinator.freeze(**fields, target=NewTurnInstallation(random_id("turn"), "revision:wrong", random_id("entry"))) is None
        assert coordinator.freeze(**fields, target=ExistingTurnInstallation(random_id("turn"), random_id("entry"))) is None
        attempt = coordinator.freeze(**fields, target=ExistingTurnInstallation(turn, random_id("entry")))
        assert attempt is not None and attempt.session_id == agent and attempt.origin_turn_id == turn
        # A true terminal Work closes its subscription while a frozen result still settles exactly.
        coordinator.close_work(turn)
        assert coordinator.list_current() == ()
        child_events = child_bus.observe(child_observer, after_revision=child_revision, maximum_events=32).events
        assert {item.event_type.value for item in child_events} >= {"TerminalMonitorOpened", "TerminalMonitorObservation", "TerminalMonitorClosed"}
        assert all(item.session_id == agent for item in child_events)
        assert not root_bus.observe(root_observer, after_revision=root_revision, maximum_events=32).events
        assert coordinator.current_installation_attempt(registration.monitor_id) is attempt
        coordinator.settle_installation(attempt, accepted=True)
        assert coordinator.current_installation_attempt(registration.monitor_id) is None
        assert registry.output_owner(state.process_id, owner_host_session_id=owner).snapshot(maximum_chars=1).process_status == "running"
    finally:
        coordinator.stop_admission_and_close(timeout_seconds=2)
        registry.release_owner(owner, timeout_seconds=5)


def test_pretool_hook_complete_argument_replacement_is_validated_and_deny_wins(tmp_path):
    import json
    from pulsara_agent.hooks.contracts import GateDecision
    from pulsara_agent.hooks.dispatcher import _aggregate
    from pulsara_agent.hooks.output_parser import parse_handler_output, ValidHandlerContribution, InvalidHandlerOutput
    from tests.test_round9_2_hook_subsystem import _definition, _execution
    definition = _definition(tmp_path, HookEventType.PRE_TOOL_USE_EVENT, "rewrite")

    def parse(value, *, asynchronous=False):
        return parse_handler_output(_execution(replace(definition, asynchronous=asynchronous),
            stdout=json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", **value}}).encode()))

    first = parse({"updatedInput": {"code": "return 1;"}})
    second = parse({"updatedInput": {"path": "script.js"}})
    assert isinstance(first, ValidHandlerContribution)
    assert _aggregate(HookEventType.PRE_TOOL_USE_EVENT, (first, second), continuation_already_used=False).updated_input == {"path": "script.js"}
    assert isinstance(parse({"updatedInput": []}), InvalidHandlerOutput)
    assert isinstance(parse({"updatedInput": {}}, asynchronous=True), InvalidHandlerOutput)
    denied = parse({"updatedInput": {"code": "return 2;"}, "permissionDecision": "deny"})
    outcome = _aggregate(HookEventType.PRE_TOOL_USE_EVENT, (first, denied), continuation_already_used=False)
    assert outcome.decision is GateDecision.BLOCK
    assert outcome.updated_input is None


def test_shared_hook_dispatch_exposes_real_subagent_session_and_instruction_origin(tmp_path):
    import asyncio
    from pulsara_agent.hooks.contracts import HookDispatchEnvelope, QueuedPromptRef, UserPromptSubmitInput, HookDispatchScopeRef
    from pulsara_agent.hooks.dispatcher import KernelHookDispatcher
    from pulsara_agent.hooks.matcher import event_matcher_subject
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
    from tests.test_round9_2_hook_subsystem import _definition, _ScriptedExecutor
    from time import monotonic

    async def scenario():
        definition = replace(_definition(tmp_path, HookEventType.USER_PROMPT_SUBMIT_EVENT, "child"), scope_policy=HookScopePolicy.SUBAGENT)
        executor = _ScriptedExecutor({"child": (0, b"work context", b"")})
        dispatcher = KernelHookDispatcher(initial_view=_view(tmp_path, (definition,)), workspace_root=tmp_path,
            credential_boundary=ProcessCredentialBoundary(), executor=executor)
        try:
            scope = HookDispatchScopeRef(object(), object(), HookScopeKind.SUBAGENT, "session:actual-child")
            public = UserPromptSubmitInput(session_id="session:actual-child", cwd=str(tmp_path), model="model:test",
                session_kind="SUBAGENT", input_origin="AGENT_INSTRUCTION", turn_id="turn:actual-work",
                prompt="actual instruction", permission_mode="bypassPermissions")
            outcome = await dispatcher.dispatch(HookDispatchEnvelope(dispatcher.capture_view(), scope, public,
                QueuedPromptRef(None, "queue:actual-work", "turn:actual-work", "entry:instruction", "revision:work"), monotonic()+10),
                matcher_subject=event_matcher_subject(HookEventType.USER_PROMPT_SUBMIT_EVENT))
            assert outcome.context_entries
            assert len(executor.calls) == 1
            sent = executor.calls[0].public_stdin
            assert sent["session_id"] == "session:actual-child"
            assert sent["session_kind"] == "SUBAGENT"
            assert sent["input_origin"] == "AGENT_INSTRUCTION"
        finally:
            await dispatcher.aclose(deadline_monotonic=monotonic()+5)

    asyncio.run(scenario())
