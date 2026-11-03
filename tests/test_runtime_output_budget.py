"""Runtime adapters use complete pages, exact next calls and physical cancellation."""

import asyncio
from dataclasses import replace
import json
from threading import Event, RLock
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.code_mode.contracts import CodeModeError
from pulsara_agent.conversation_kernel.execution_watchdogs import KernelExecutionDeadlineFactory
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort, _invocation_page_budget
from pulsara_agent.conversation_kernel.tool_surface import BuiltinExecutionPolicyRef
from pulsara_agent.identity import random_id
from pulsara_agent.message import ToolResultState
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.ports.tool_execution import ToolExecutionResult, ToolOutputSourceFormatHint
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBoundary, ToolResultPageBudget


def context(*, maximum=None):
    call_id = random_id("call")
    session_id = random_id("session")
    return SimpleNamespace(session_id=session_id, tool_call_id=call_id,
        page_budget=ToolResultPageBudget(call_id, result_target={"kind": "SESSION_ENTRY", "session_id": session_id,
            "entry_id": random_id("entry")}, accepts_provider_body=(
            None if maximum is None else lambda body, _ids: len(body.encode()) <= maximum)))


def test_cell_source_query_measures_escaped_carrier_and_returns_copyable_next():
    async def run():
        port = object.__new__(DirectKernelToolPort)
        identity = random_id("cell")
        source = "\\" * 20_000
        capture = SimpleNamespace(source_exec_attempt_id=random_id("attempt"),
            public_value=lambda: {"target": {"kind": "CELL", "cell_id": identity}, "state": "completed"})
        port.capture_code_cell = lambda _identity: capture
        async def read(**kwargs):
            return source
        port._code_mode_read_source = read
        ctx = context()
        arguments = {"target": {"kind": "CELL", "cell_id": identity}, "max_chars": 20_000}
        pieces = []
        while arguments is not None:
            value = await port._query_cells(arguments, ctx)
            assert ctx.page_budget.fits(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            page = value["source"]
            assert "has_more" not in page and "next_offset_chars" not in page
            pieces.append(page["text"])
            next_call = value["next"]
            arguments = None if next_call is None else next_call["arguments"]
            if next_call is not None:
                assert next_call["tool"] == "query_cells"
                assert arguments["offset_chars"] == sum(map(len, pieces))
        assert len(pieces) > 1
        assert "".join(pieces) == source
    asyncio.run(run())


def test_cell_inventory_packs_atomic_rows_and_complete_next():
    async def run():
        port = object.__new__(DirectKernelToolPort)
        rows = [{"target": {"kind": "CELL", "cell_id": random_id("cell")}, "state": "running"} for _ in range(10)]
        port.query_code_cells = lambda **kwargs: {"cells": rows, "has_more": False, "next_cursor": None}
        ctx = context(maximum=400)
        value = await port._query_cells({"max_items": 10}, ctx)
        assert 0 < len(value["cells"]) < len(rows)
        assert value["next"] == {"tool": "query_cells", "arguments": {
            "cursor": value["cells"][-1]["target"]["cell_id"], "max_items": 10}}
        assert ctx.page_budget.fits(json.dumps(value, separators=(",", ":")))
        with pytest.raises(CodeModeError, match="inventory row"):
            await port._query_cells({"max_items": 10}, context(maximum=1))
    asyncio.run(run())


def test_native_budget_missing_is_not_replaced_with_physical_fallback():
    ctx = SimpleNamespace(tool_call_id=random_id("call"),
                          surface_borrow=SimpleNamespace(code_cell_id=None))
    with pytest.raises(ToolResultPageBoundary, match="allocated"):
        _invocation_page_budget(ctx)


def test_model_cell_stop_preserves_exact_short_receipt_without_ui_pending_count():
    async def run():
        ctx = context()
        identity = random_id("cell")
        internal = {"target": {"kind": "CELL", "cell_id": identity}, "state": "completed", "pending_calls": 7}
        async def stop(cell_id, *, page_budget):
            assert cell_id == identity and page_budget is ctx.page_budget
            return internal
        port = object.__new__(DirectKernelToolPort)
        port._code_mode = SimpleNamespace(stop=stop)
        result = await port._invoke_code_mode_control("manage_cell", {"action": "stop", "target": internal["target"]}, ctx)
        expected = {"target": internal["target"], "state": "completed"}
        assert result.state == "SUCCESS" and result.page_complete
        assert json.loads(result.content) == result.required_facts == expected
        assert internal["pending_calls"] == 7
    asyncio.run(run())


@pytest.mark.parametrize("operation", ("exec_cell", "manage_cell"))
def test_cell_control_budget_failure_returns_known_no_effect_result(operation):
    from pulsara_agent.code_mode.host import CodeModeHost
    async def run():
        ctx = context(maximum=1)
        ctx.__dict__.update(attempt_id=random_id("attempt"), turn_id=random_id("turn"),
            code_mode_snapshot=SimpleNamespace(tool_directory=SimpleNamespace(tool_specs=())))
        host = CodeModeHost()
        port = object.__new__(DirectKernelToolPort)
        port._code_mode = host
        async def unused(*args, **kwargs):
            return None
        port._code_mode_invoke = port._code_mode_freeze_source = unused
        identity = random_id("cell")
        cell = SimpleNamespace(cell_id=identity, state="running", pending_calls=7,
                               explicitly_stopped=False, cancelled=asyncio.Event())
        if operation == "manage_cell":
            host._cells[identity] = cell
        try:
            arguments = {"code": "throw new Error('must not execute');"} if operation == "exec_cell" else {
                "action": "stop", "target": {"kind": "CELL", "cell_id": identity}}
            result = await port._invoke_code_mode_control(operation, arguments, ctx)
            expected = {"error": "OUTPUT_PAGE_BOUNDARY", "physical_outcome": "NO_EFFECT", "retry_performed": False}
            assert result.state == "APPLICATION_ERROR" and json.loads(result.content) == result.required_facts == expected
            assert host._process is None
            if operation == "exec_cell":
                assert not host._cells
            else:
                assert host._cells[identity] is cell and not cell.explicitly_stopped and not cell.cancelled.is_set()
        finally:
            host._cells.clear()
            await host.aclose()
    asyncio.run(run())


def test_mcp_inspect_quotes_actual_ref_and_rejects_without_leaking_prepared_ref(monkeypatch):
    from pulsara_agent.capability.contracts import CapabilityKind, CapabilitySourceKind, capability_identity, capability_source_ref
    from pulsara_agent.capability.mcp_projection import McpInspectionDescriptorValues
    from pulsara_agent.conversation_kernel import tool_runtime as runtime
    from pulsara_agent.conversation_kernel.mcp.meta import ProcessLocalNewMcpToolRefOwner
    from pulsara_agent.identity import require_id
    from pulsara_agent.primitives.context import freeze_json
    identity = capability_identity(kind=CapabilityKind.TOOL,
        source=capability_source_ref(CapabilitySourceKind.MCP_SERVER, "fixture"), stable_name="inspect")
    monkeypatch.setattr(runtime, "_mcp_executor_capability_version", lambda _executor: SimpleNamespace(identity=identity, semantic_fingerprint="schema"))
    monkeypatch.setattr(runtime, "execution_policy_fingerprint", lambda _policy: "policy")
    monkeypatch.setattr(runtime, "_mcp_inspection_values", lambda _executor: McpInspectionDescriptorValues(
        "fixture", "inspect", "mcp__fixture__inspect", "Read schema.", freeze_json({"type": "object"}), None, "READ_ONLY"))
    quoted = []
    def reject(body, _ids):
        quoted.append(json.loads(body)["invocation"]["target"]["tool_ref"])
        return False
    port = object.__new__(DirectKernelToolPort)
    port._mcp_meta_refs = ProcessLocalNewMcpToolRefOwner()
    port._mcp_ref_settlements = {}
    port._mcp_ref_consumer_for_borrow = lambda _borrow: {
        "session_id": random_id("session"), "continuity_epoch_nonce": random_id("epoch"),
        "conversation_scope_kind": ModelInputScopeKind.ROOT}
    ctx = SimpleNamespace(surface_borrow=SimpleNamespace(code_cell_id=None), result_entry_id=random_id("entry"),
        page_budget=ToolResultPageBudget(random_id("call"), accepts_provider_body=reject))
    result = port._prepare_mcp_inspection_result(executor=SimpleNamespace(policy=object(), runtime_generation_id=1),
        route_fingerprint="route", invocation_context=ctx)
    assert result.state == "APPLICATION_ERROR"
    assert json.loads(result.content)["status"] == "MCP_DESCRIPTOR_OVERBOUND"
    assert len(quoted) == 1 and require_id(quoted[0], "mcpref") == quoted[0]
    assert port._mcp_meta_refs._refs == {} and port._mcp_meta_refs._prepared == {}
    assert port._mcp_ref_settlements == {}


def test_mcp_ref_install_uses_exact_request_and_target_even_when_provider_call_id_repeats(monkeypatch):
    from pulsara_agent.conversation_kernel import tool_runtime as runtime
    from pulsara_agent.model_input.contracts import ToolResultProviderRenderMode
    from pulsara_agent.primitives.context import freeze_json
    monkeypatch.setattr(runtime, "compiled_tool_result_source_fingerprint", lambda item: item.source_entry_id)
    session = random_id("session")
    request_one, request_two, inspect_result, read_result = (random_id("entry") for _ in range(4))
    shared_call_id = "provider-reused-call"
    def request(identity, name, arguments):
        return SimpleNamespace(source_entry_id=identity, tool_call_id=None,
            tool_calls=(SimpleNamespace(tool_call_id=shared_call_id, tool_name=name, arguments=freeze_json(arguments)),))
    def result(identity, owner):
        return SimpleNamespace(source_entry_id=identity, tool_call_id=shared_call_id,
            tool_request_entry_id=owner, tool_calls=())
    items = [request(request_one, "query_capabilities", {"target": {"kind": "MCP_TOOL", "server_id": "fixture", "tool_name": "probe"}}),
             result(inspect_result, request_one), request(request_two, "read_file", {"target": {"kind": "PATH", "path": "note"}}),
             result(read_result, request_two)]
    access = SimpleNamespace(conversation_scope_kind=ModelInputScopeKind.ROOT, session_id=session, surface_generation=1)
    borrow = SimpleNamespace(borrow_id=random_id("borrow"), prepared=SimpleNamespace(access=access, capability_exposure_plan=None))
    port = object.__new__(DirectKernelToolPort)
    port._session_id, port._surface_lock = session, RLock()
    port.validate_tool_surface_borrow = lambda *args: None
    port._surface_borrows, port._installed_epoch_by_borrow = {borrow.borrow_id: 1}, {}
    port._mcp_runtime_by_surface_generation = {}
    installed = []
    port._mcp_meta_refs = SimpleNamespace(retire_scope_except_epoch=lambda **kwargs: None,
        retire_unroutable=lambda **kwargs: None, install_full_result=lambda **kwargs: installed.append(kwargs["result_entry_id"]))
    permit = SimpleNamespace(scope=SimpleNamespace(session_id=session, scope_kind=ModelInputScopeKind.ROOT),
                             epoch_nonce=random_id("epoch"), epoch_revision=1)
    decisions = [SimpleNamespace(source_entry_fingerprint=identity, selected_mode=ToolResultProviderRenderMode.FULL)
                 for identity in (inspect_result, read_result)]
    port.install_provider_input_tool_result_deliveries(permit=permit,
        canonical_facts=SimpleNamespace(canonical_input=SimpleNamespace(items=items)),
        compiled_input=SimpleNamespace(tool_result_decisions=decisions), surface_borrow=borrow)
    assert installed == [inspect_result]


def test_runtime_cancellation_reaches_synchronous_tool_and_joins_cleanup():
    async def run():
        started, cleaned = Event(), Event()
        ctx = context()
        class CancellableTool:
            def execute(self, call):
                assert call.page_budget is ctx.page_budget
                started.set()
                while not call.cancel_requested():
                    cleaned.wait(0.001)
                cleaned.set()
                return ToolExecutionResult(call.id, call.name, ToolResultState.SUCCESS, '{"finished":true}',
                    page_complete=True, public_format=ToolOutputSourceFormatHint.JSON)
        port = object.__new__(DirectKernelToolPort)
        binding = SimpleNamespace(tool_name="find_files", execution_policy=BuiltinExecutionPolicyRef("find_files", "fixture"))
        borrow = SimpleNamespace(code_cell_id=None,
            prepared=SimpleNamespace(access=SimpleNamespace(conversation_scope_kind=ModelInputScopeKind.ROOT)),
            execution_binding=lambda name: binding)
        ctx.__dict__.update(turn_id=random_id("turn"), assistant_entry_id=random_id("entry"), attempt_id=random_id("attempt"),
            attempt_permission_snapshot_fingerprint="permission", permission_snapshot_fingerprint="permission",
            surface_borrow=borrow, conversation_scope_kind="ROOT", cell_cancel_event=None,
            code_cell_id=None, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            permission_confirmation_granted=False)
        port._closed, port._session_id = False, ctx.session_id
        port._validate_surface_borrow = lambda *args: binding
        port._subagent, port._memory = None, None
        port._tools = {"find_files": CancellableTool()}
        port._deadlines = KernelExecutionDeadlineFactory()
        io = port._physical_io = KernelSessionIO()
        try:
            task = asyncio.create_task(port.invoke(tool_name="find_files", arguments={}, tool_call_id=ctx.tool_call_id,
                attempt_id=ctx.attempt_id, turn_id=ctx.turn_id, assistant_entry_id=ctx.assistant_entry_id, invocation_context=ctx))
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            result = await asyncio.wait_for(task, 2)
            assert cleaned.is_set()
            assert result.caller_cancelled_while_running
            assert result.state == "SUCCESS" and result.page_complete
        finally:
            await io.aclose(deadline_monotonic=monotonic() + 2)
    asyncio.run(run())


@pytest.mark.parametrize("action", ["switch", "delete"])
def test_schedule_mutation_keeps_complete_short_receipt_as_required_facts(action):
    async def run():
        ctx = context()
        target = {"kind": "SCHEDULE", "task_id": random_id("schedule")}
        receipt = ({"action": "delete", "target": target, "deleted": True} if action == "delete" else {
            "action": "switch", "paused": {"target": target, "revision": 3, "status": "PAUSED"},
            "active": {"target": {"kind": "SCHEDULE", "task_id": random_id("schedule")}, "revision": 1, "status": "ACTIVE"},
        })
        binding = SimpleNamespace(tool_name="manage_schedule", execution_policy=BuiltinExecutionPolicyRef("manage_schedule", "fixture"))
        borrow = SimpleNamespace(code_cell_id=None,
            prepared=SimpleNamespace(access=SimpleNamespace(conversation_scope_kind=ModelInputScopeKind.ROOT)),
            execution_binding=lambda name: binding)
        ctx.__dict__.update(turn_id=random_id("turn"), assistant_entry_id=random_id("entry"), attempt_id=random_id("attempt"),
            attempt_permission_snapshot_fingerprint="permission", permission_snapshot_fingerprint="permission",
            surface_borrow=borrow, conversation_scope_kind="ROOT", cell_cancel_event=None,
            code_cell_id=None, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
        port = object.__new__(DirectKernelToolPort)
        port._closed, port._session_id = False, ctx.session_id
        port._validate_surface_borrow = lambda *args: binding
        async def manage(arguments, *, default_session_id, permission_mode, page_budget):
            assert default_session_id == ctx.session_id
            assert permission_mode is ctx.effective_permission_mode
            assert page_budget is ctx.page_budget
            return receipt
        port._scheduled_tasks = SimpleNamespace(invoke_manage=manage)
        result = await port.invoke(tool_name="manage_schedule", arguments={"action": action},
            tool_call_id=ctx.tool_call_id, attempt_id=ctx.attempt_id, turn_id=ctx.turn_id,
            assistant_entry_id=ctx.assistant_entry_id, invocation_context=ctx)
        assert result.state == "SUCCESS" and result.page_complete
        assert json.loads(result.content) == receipt
        assert result.required_facts == receipt
    asyncio.run(run())


def test_todo_budget_rejection_precedes_prepared_or_installed_change(tmp_path):
    from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
    from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
    from tests.support.round3 import direct_tool_invocation_context
    from tests.test_lightweight_todo_refinement import _root_activation
    async def run():
        session = "session:cbauiajfisf4gvpdotrq4qgxhq"
        port = DirectKernelToolPort(workspace_root=tmp_path, host_owner_id=random_id("host"), session_id=session,
            live_bus=LiveAgentEventBus(), authorization_policy=DefaultToolDispatchAuthorizationPolicy())
        port.todo_owner.activate_root_run(_root_activation())
        ids = {"tool_name": "todo", "tool_call_id": random_id("call"), "attempt_id": random_id("attempt"),
               "turn_id": "turn:nzc36cttgo7xxki7bcjc7ops7a", "assistant_entry_id": random_id("entry")}
        borrow, ctx = direct_tool_invocation_context(port, session_id=session, **ids)
        ctx = replace(ctx, page_budget=replace(ctx.page_budget, accepts_provider_body=lambda *_: False))
        before = port.todo_owner.current_snapshots()
        try:
            result = await port.invoke(**ids, arguments={"items": [{"text": "Must not install", "status": "pending"}]}, invocation_context=ctx)
            assert result.state == "APPLICATION_ERROR"
            assert json.loads(result.content) == result.required_facts == {
                "error": "OUTPUT_PAGE_BOUNDARY", "physical_outcome": "NO_EFFECT", "retry_performed": False}
            assert port.todo_owner.current_snapshots() == before
            assert port._todo_settlements == {}
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)
    asyncio.run(run())


@pytest.mark.parametrize("route", ("META", "DIRECT", "RESOURCE"))
def test_mcp_minimum_budget_rejection_releases_permit_without_remote_dispatch(monkeypatch, route):
    from pulsara_agent.conversation_kernel import tool_runtime as runtime
    from pulsara_agent.conversation_kernel.tool_surface import McpEffectKind, McpPolicyClassificationSource, McpToolExecutionPolicyFact
    from pulsara_agent.primitives.context import freeze_json
    async def run():
        name = {"META": "use_new_mcp_tool", "DIRECT": "mcp__fixture__call", "RESOURCE": "read_mcp_resource"}[route]
        ctx = context(maximum=1)
        policy = McpToolExecutionPolicyFact(server_id="fixture", remote_tool_name="call", provider_tool_name=name,
            tool_semantic_fingerprint="schema", effect_kind=McpEffectKind.EXTERNAL_EFFECT,
            timeout_ms=1000, parallel_safe=False, classification_source=McpPolicyClassificationSource.HOST_DEFAULT)
        called = []
        class Executor:
            def __init__(self):
                self.policy = policy
            async def invoke(self, *args, **kwargs):
                called.append("remote")
                raise AssertionError("remote operation must not start")
        class Runtime:
            async def invoke_standard(self, **kwargs):
                called.append("resource")
                raise AssertionError("remote operation must not start")
        monkeypatch.setattr(runtime, "McpBoundToolExecutor", Executor)
        monkeypatch.setattr(runtime, "McpInstalledRuntimeGeneration", Runtime)
        executor = Executor()
        binding = SimpleNamespace(tool_name=name, execution_policy=policy if route == "DIRECT" else BuiltinExecutionPolicyRef(name, "fixture"))
        borrow = SimpleNamespace(code_cell_id=None,
            prepared=SimpleNamespace(access=SimpleNamespace(conversation_scope_kind=ModelInputScopeKind.ROOT, surface_generation=1)),
            execution_binding=lambda tool: binding)
        ctx.__dict__.update(turn_id=random_id("turn"), assistant_entry_id=random_id("entry"), attempt_id=random_id("attempt"),
            attempt_permission_snapshot_fingerprint="permission", permission_snapshot_fingerprint="permission",
            surface_borrow=borrow, conversation_scope_kind="ROOT", cell_cancel_event=None,
            code_cell_id=None, effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
        permit = SimpleNamespace(state=SimpleNamespace(value="ADMITTED"),
            release=lambda: called.append("release"), mark_attempt_accepted=lambda: called.append("accepted"))
        port = object.__new__(DirectKernelToolPort)
        port._closed, port._session_id = False, ctx.session_id
        port._validate_surface_borrow = lambda *args: binding
        key = (1, ctx.tool_call_id)
        port._mcp_dispatch_permits = {key: permit}
        port._mcp_dispatch_resources = {key: Runtime() if route == "RESOURCE" else executor}
        port._mcp_meta_invocations = {key: SimpleNamespace(arguments=freeze_json({}), executor=executor)}
        arguments = {"arguments": {}} if route == "META" else {}
        result = await port.invoke(tool_name=name, arguments=arguments, tool_call_id=ctx.tool_call_id,
            attempt_id=ctx.attempt_id, turn_id=ctx.turn_id, assistant_entry_id=ctx.assistant_entry_id, invocation_context=ctx)
        facts = {"error": "OUTPUT_PAGE_BOUNDARY", "physical_outcome": "NO_EFFECT", "retry_performed": False}
        assert result.state == "APPLICATION_ERROR" and json.loads(result.content) == facts
        assert result.required_facts == facts
        assert called == ["release"] and not port._mcp_dispatch_permits and not port._mcp_dispatch_resources
    asyncio.run(run())
