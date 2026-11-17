"""Web dispatch keeps the ordinary role, permission, complete-result and Cell paths."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from pulsara_agent.code_mode import CodeModeHost, CodeModeToolResult
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.native_tool_delivery import (
    NativeResultPosition, project_native_tool_page, resolve_tool_result_next,
)
from pulsara_agent.conversation_kernel.tool_contracts import KernelToolAuthorizationKind
from pulsara_agent.conversation_kernel.tool_policy import DefaultToolDispatchAuthorizationPolicy
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.tool_outputs import project_tool_result_export
from pulsara_agent.identity import random_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.ports.tool_execution import thaw_tool_json_object
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.primitives.run_permission import build_run_permission_snapshot, RunPermissionAdmissionSource
from pulsara_agent.web.contracts import WEB_TOOL_NAMES, web_result_schema
from tests.support.round3 import direct_tool_invocation_context, prepare_test_direct_tool_surface

TARGET = {"kind": "URL", "url": "https://example.org/page"}
SEARCH = {"meta": {"status": "SUCCESS"}, "content": [{"type": "object", "value": {
    "target": TARGET, "title": "Source", "snippet": "Actual provider preview", "date": "2026-11-16"}}]}
FETCH = {"target": TARGET, "meta": {"status": "SUCCESS", "title": "Actual title"},
    "content": [{"type": "text", "value": "# Body\r\nOriginal content\n"}]}


class MockWebService:
    def __init__(self, values=None):
        self.values = values or {"web_search": SEARCH, "web_fetch": FETCH}
        self.calls = []

    async def execute(self, name, arguments, *, deadline_monotonic=None):
        self.calls.append((name, deepcopy(arguments), deadline_monotonic))
        return deepcopy(self.values[name])


def port_for(tmp_path, service, *, session_id=None):
    port = DirectKernelToolPort(workspace_root=tmp_path, host_owner_id=random_id("host"),
        session_id=session_id or random_id("session"), live_bus=LiveAgentEventBus(), scheduled_tasks=object(),
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(), web_service=service)
    port.bind_subagent_port(SimpleNamespace(tool_names=(), bind_cell_observer=lambda _observer: None))
    return port


def permission(mode):
    return build_run_permission_snapshot(snapshot_id=random_id("permission"), requested_mode=mode,
        effective_mode=mode, admission_source=RunPermissionAdmissionSource.USER_SUBMISSION)


async def dispatch(port, name, arguments, *, scope=ModelInputScopeKind.ROOT,
                   mode=PermissionMode.BYPASS_PERMISSIONS, cancel_event=None, snapshot=None):
    call, attempt, turn, entry = [random_id(kind) for kind in ("call", "attempt", "turn", "entry")]
    snapshot = snapshot or permission(mode)
    borrow, context = direct_tool_invocation_context(port, session_id=port._session_id,
        tool_name=name, tool_call_id=call, attempt_id=attempt, turn_id=turn,
        assistant_entry_id=entry, conversation_scope_kind=scope, permission_snapshot=snapshot)
    context = replace(context, cell_cancel_event=cancel_event)
    try:
        prepared = port.prepare_resolved_invocation(tool_name=name, arguments=arguments, surface_borrow=borrow)
        assert prepared.canonical_tool_name == name
        authorization = await port.authorize(
            tool_name=name, arguments=arguments, tool_call_id=call, turn_id=turn, assistant_entry_id=entry,
            permission_snapshot=snapshot, surface_borrow=borrow, memory_context=context.memory_context)
        assert authorization.kind is KernelToolAuthorizationKind.ALLOW
        result = await port.invoke(tool_name=name, arguments=arguments, tool_call_id=call,
            attempt_id=attempt, turn_id=turn, assistant_entry_id=entry, invocation_context=context)
        Draft202012Validator(web_result_schema(name)).validate(result.public_value())
        return result
    finally:
        borrow.close()


@pytest.mark.parametrize("scope", [ModelInputScopeKind.ROOT, ModelInputScopeKind.SUBAGENT])
@pytest.mark.parametrize("mode", list(PermissionMode))
def test_web_native_roles_and_permissions_include_readonly_plan(tmp_path, scope, mode):
    async def run():
        service = MockWebService()
        port = port_for(tmp_path, service)
        try:
            surface = prepare_test_direct_tool_surface(port, conversation_scope_kind=scope,
                effective_permission_mode=mode)
            assert WEB_TOOL_NAMES <= {spec.name for spec in surface.model_surface.tool_specs}
            search = await dispatch(port, "web_search", {"query": " original query "}, scope=scope, mode=mode)
            fetched = await dispatch(port, "web_fetch", {"target": TARGET}, scope=scope, mode=mode)
            assert search.public_value() == SEARCH and fetched.public_value() == FETCH
            assert service.calls[0][1] == {"query": " original query "}
            assert all(call[2] is not None for call in service.calls)
        finally:
            await port.aclose()
    asyncio.run(run())


def test_fixed_js_directory_and_complete_values_survive_quickjs_bridge(tmp_path):
    async def run():
        long = "正文🙂\r\n" * 18000
        failure = {"target": TARGET, "meta": {"status": "ERROR"}, "content": [
            {"type": "text", "value": "actual partial text\n"},
            {"type": "text", "value": "extraction interrupted"}]}
        service = MockWebService({"web_search": SEARCH,
            "web_fetch": {**FETCH, "content": [{"type": "text", "value": long}]}})
        port, host = port_for(tmp_path, service), CodeModeHost()
        delivered, expected = [], []
        try:
            surface = prepare_test_direct_tool_surface(port)
            directory = port.freeze_code_mode_tool_directory(surface,
                effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
            catalog = {spec.name: {"parameters": thaw_json(spec.parameters)} for spec in directory.tool_specs}
            assert WEB_TOOL_NAMES <= catalog.keys()
            # Account/readiness changes cannot rewrite a sealed catalog.
            before = tuple(directory.tool_specs)

            async def invoke(call):
                if call.arguments.get("target", {}).get("url", "").endswith("failure"):
                    service.values["web_fetch"] = failure
                result = await dispatch(port, call.tool_name, call.arguments)
                value = result.public_value()
                expected.append(value)
                return CodeModeToolResult(value=value, state=result.state,
                    error_code="APPLICATION_ERROR" if result.state != "SUCCESS" else None,
                    error_message="actual extraction failed" if result.state != "SUCCESS" else None)

            cell = await host.start(source='const hits=await tools.web_search({query:"source"});'
                'console.log(hits);console.log(await tools.web_fetch({target:hits.content[0].value.target}));'
                'try{await tools.web_fetch({target:{kind:"URL",url:"https://example.org/failure"}})}'
                'catch(err){console.log(err.result)}',
                source_exec_attempt_id=random_id("attempt"), turn_id=random_id("turn"),
                snapshot=object(), decision_available=False, tool_catalog=catalog, invoke=invoke)
            state = await host.wait_state(cell, 30000)
            assert state["state"] == "completed", state
            while lease := await host.lease_output(cell):
                delivered.append(thaw_tool_json_object(lease.record)["values"][0]["value"])
                await host.acknowledge_output(lease)
                if cell not in host._cells:
                    break
            assert delivered == expected
            assert delivered[1]["content"][0]["value"] == long
            assert delivered[2] == failure
            assert port.freeze_code_mode_tool_directory(prepare_test_direct_tool_surface(port),
                effective_permission_mode=PermissionMode.BYPASS_PERMISSIONS).tool_specs == before
        finally:
            await host.aclose()
            await port.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("status", ["SUCCESS", "ERROR"])
def test_late_cell_stop_preserves_confirmed_business_outcome(tmp_path, status):
    async def run():
        stop = asyncio.Event()
        value = {**FETCH, "meta": {"status": status}, "content": [{"type": "text", "value": "known result"}]}
        class Service:
            async def execute(self, *args, **kwargs):
                stop.set()
                return value
        port = port_for(tmp_path, Service())
        try:
            result = await dispatch(port, "web_fetch", {"target": TARGET}, cancel_event=stop)
            assert result.public_value() == value
            assert result.caller_cancelled_while_running
        finally:
            await port.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("cancel_source", ["turn", "cell"])
def test_actual_cancel_joins_network_and_keeps_partial_failure_value(tmp_path, cancel_source):
    async def run():
        started, stop, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        partial = {"target": TARGET, "meta": {"status": "CANCELLED"},
            "content": [{"type": "text", "value": "known partial extraction"}]}
        class Service:
            async def execute(self, *args, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    error.result = partial
                    raise
                finally:
                    finished.set()
        port = port_for(tmp_path, Service())
        try:
            task = asyncio.create_task(dispatch(port, "web_fetch", {"target": TARGET}, cancel_event=stop))
            await started.wait()
            task.cancel() if cancel_source == "turn" else stop.set()
            result = await asyncio.wait_for(task, 2)
            assert result.public_value() == partial and finished.is_set()
            assert not port._web_tasks
        finally:
            await port.aclose()
    asyncio.run(run())


def test_web_fetch_next_reads_frozen_text_without_fetching_or_line_numbers(tmp_path):
    from pulsara_agent.ports.tool_output import NativeToolContinuation, ToolResultView
    async def run():
        raw = "# Source\r\n2|literal source line\n" * 12000
        service = MockWebService({"web_fetch": {**FETCH, "content": [{"type": "text", "value": raw}]}})
        port = port_for(tmp_path, service)
        try:
            result = await dispatch(port, "web_fetch", {"target": TARGET})
            value = result.public_value()
            entry = random_id("entry")
            source = ToolResultView({"kind": "SESSION_ENTRY", "entry_id": entry}, value,
                random_id("result"), "web_fetch", {"target": TARGET})
            def page_at(position):
                return project_native_tool_page(session_id=port._session_id, entry_id=entry,
                    value=source.value, source_tool_name=source.source_tool_name,
                    source_arguments=source.source_arguments, start=NativeResultPosition(*position), image_reader=lambda _: None)
            def read_native_page(target):
                selected, position = resolve_tool_result_next(target, session_id=port._session_id)
                assert selected == entry
                return NativeToolContinuation(source, (position.segment, position.offset))
            port._tools["read_file"].result_source_reader = SimpleNamespace(read_native_page=read_native_page)
            first = page_at((0, 0))
            assert first.next is not None and first.estimated_tokens <= 12000
            pages = [first]
            while pages[-1].next is not None:
                continuation = await dispatch_read_file(port, {"target": dict(pages[-1].next)})
                pages.append(page_at(continuation.position))
            assert len(pages) > 2
            assert "".join(page.body for page in pages).encode() == project_tool_result_export(value)
            assert len(service.calls) == 1
            assert raw in project_tool_result_export(value).decode()
        finally:
            await port.aclose()
    asyncio.run(run())


async def dispatch_read_file(port, arguments):
    from tests.test_query_runtime_integration import dispatch as ordinary_dispatch
    result = await ordinary_dispatch(port, "read_file", arguments)
    assert result.state == "SUCCESS"
    return result.native_continuation


def test_plan_overlay_preserves_native_web_read_admission(tmp_path):
    from pulsara_agent.primitives.run_permission import RunPermissionOverlay
    async def run():
        service = MockWebService()
        port = port_for(tmp_path, service)
        snapshot = build_run_permission_snapshot(snapshot_id=random_id("permission"),
            requested_mode=PermissionMode.BYPASS_PERMISSIONS, effective_mode=PermissionMode.READ_ONLY,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
            overlay=RunPermissionOverlay.PLAN_READ_ONLY, plan_context_ordinal_at_admission=1,
            plan_workflow_id=random_id("plan"), plan_workflow_revision_at_admission=1)
        try:
            assert (await dispatch(port, "web_search", {"query": "plan research"}, snapshot=snapshot)).public_value() == SEARCH
            assert (await dispatch(port, "web_fetch", {"target": TARGET}, snapshot=snapshot)).public_value() == FETCH
        finally:
            await port.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["cancel", "deadline"])
@pytest.mark.parametrize("consumer", ["native", "js"])
def test_real_service_partial_fallback_cancellation_and_deadline_keep_obtained_text(tmp_path, outcome, consumer):
    import time
    import httpx
    from pulsara_agent.settings import LocalSettings
    from pulsara_agent.web.service import WebService

    async def run():
        entered, cancel = asyncio.Event(), asyncio.Event()
        calls = []
        async def transport(request):
            calls.append(request.url.host)
            if len(calls) == 1:
                return httpx.Response(200, json={"success": True, "data": {
                    "markdown": "known first partial body\n", "metadata": {"statusCode": 503, "title": "Known title"}}})
            entered.set()
            await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            service = WebService(SimpleNamespace(read=lambda: LocalSettings()), client)
            port, host = port_for(tmp_path, service), CodeModeHost()
            if outcome == "deadline":
                port._deadlines = SimpleNamespace(deadline=lambda _owner: time.monotonic() + .08)
            expected_status = "CANCELLED" if outcome == "cancel" else "ERROR"
            async def do_call():
                return await dispatch(port, "web_fetch", {"target": TARGET}, cancel_event=cancel)
            task = None
            try:
                if consumer == "native":
                    task = asyncio.create_task(do_call())
                else:
                    async def invoke(_call):
                        result = await do_call()
                        return CodeModeToolResult(value=result.public_value(), state=result.state,
                            error_code=result.state, error_message="web request did not complete")
                    cell = await host.start(source='try{await tools.web_fetch({target:{kind:"URL",url:"https://example.org/page"}})}catch(err){console.log(err.result)}',
                        source_exec_attempt_id=random_id("attempt"), turn_id=random_id("turn"),
                        snapshot=object(), decision_available=False,
                        tool_catalog={"decide": {}, "web_fetch": {}}, invoke=invoke)
                await asyncio.wait_for(entered.wait(), 2)
                if outcome == "cancel":
                    cancel.set()
                if consumer == "native":
                    result = await asyncio.wait_for(task, 2)
                    value = result.public_value()
                else:
                    state = await host.wait_state(cell, 30000)
                    assert state["state"] == "completed", state
                    lease = await host.lease_output(cell)
                    value = thaw_tool_json_object(lease.record)["values"][0]["value"]
                    await host.acknowledge_output(lease)
                assert value["meta"] == {"status": expected_status}
                assert value["target"] == TARGET
                assert value["content"][0] == {"type": "text", "value": "known first partial body\n"}
                assert "Known title" in value["content"][1]["value"]
                if outcome == "deadline":
                    assert "deadline" in value["content"][-1]["value"]
                assert calls == ["api.firecrawl.dev", "api.tavily.com"]
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await host.aclose()
                await port.aclose()
    asyncio.run(run())


def test_core_owns_one_readonly_web_client_across_ports_and_closes_it_at_shutdown(tmp_path):
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from tests.support.model_config import test_model_runtime
    async def run():
        core = KernelHostCore.production(model_runtime=test_model_runtime())
        service = core._web_service
        assert service._settings is core._model_runtime.settings
        assert service._boundary is core._credential_boundary
        root, child = port_for(tmp_path, service), port_for(tmp_path, service)
        try:
            assert root._web_service is child._web_service is service
            await child.aclose()
            assert not service._client.is_closed
            await root.aclose()
            assert not service._client.is_closed
            await core.shutdown()
            assert service._client.is_closed
        finally:
            await child.aclose()
            await root.aclose()
            await core.shutdown()
    asyncio.run(run())
