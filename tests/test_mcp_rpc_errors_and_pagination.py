"""Real SDK discovery and error responses from a local protocol peer."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import sys

import pytest

from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor, discover_mcp_catalog
from pulsara_agent.conversation_kernel.mcp.sdk_facade import BoundedMcpSdkClient, MCPError
from pulsara_agent.mcp_config import _parse_server
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary


_SERVER = '''
import json,os,sys
options = json.loads(OPTIONS)
requests = []
counts = {}
listings = {
    'tools/list': ('tools', [
        {'name':'first','inputSchema':{'type':'object'}},
        {'name':'second','inputSchema':{'type':'object'}},
    ]),
    'resources/list': ('resources', [
        {'name':'first','uri':'fixture://first'},
        {'name':'second','uri':'fixture://second'},
    ]),
    'resources/templates/list': ('resourceTemplates', [
        {'name':'first','uriTemplate':'fixture://first/{key}'},
        {'name':'second','uriTemplate':'fixture://second/{key}'},
    ]),
    'prompts/list': ('prompts', [{'name':'first'},{'name':'second'}]),
}
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    requests.append(request)
    with open('requests.json','w') as output:
        json.dump(requests,output)
    method = request['method']
    counts[method] = counts.get(method,0)+1
    response = {'jsonrpc':'2.0','id':request['id']}
    if method == 'server/discover' and options.get('legacy'):
        response['error'] = {'code':-32601,'message':'Method not found'}
        print(json.dumps(response),flush=True)
        continue
    if method == options.get('error_method') and counts[method] == options.get('error_on_page', 1):
        response['error'] = {
            'code':options['error_code'], 'message':'requested operation rejected',
            'data':{'reason':'server rejected request','retryHint':'fix parameters'},
        }
        if options.get('echo_secret'):
            response['error']['data']['credential'] = os.environ['MCP_TOKEN']
        print(json.dumps(response),flush=True)
        continue
    if method == 'server/discover':
        result = {'supportedVersions':['2026-07-28'],
                  'capabilities':{'tools':{},'resources':{},'prompts':{}}}
    elif method == 'initialize':
        result = {'protocolVersion':'2025-11-25',
                  'capabilities':{'tools':{},'resources':{},'prompts':{}},
                  'serverInfo':{'name':'legacy-probe','version':'1'}}
    elif method in listings:
        field,items = listings[method]
        if method == options.get('pagination_method'):
            params = request.get('params') or {}
            result = {field:items[1:] if 'cursor' in params else items[:1]}
            if 'cursor' not in params:
                result['nextCursor'] = ''
            else:
                assert params['cursor'] == ''
        else:
            result = {field:items[:1]}
    elif method == 'tools/call':
        result = {'content':[{'type':'text','text':'same connection success'}]}
    elif method == 'resources/read':
        result = {'contents':[{'uri':request['params']['uri'],'text':'same connection success'}]}
    elif method == 'prompts/get':
        result = {'messages':[{'role':'user','content':{'type':'text','text':'same connection success'}}]}
    else:
        raise RuntimeError(method)
    if not options.get('legacy') and (method == 'server/discover' or method in listings):
        result.update(cacheScope='private',ttlMs=0)
    response['result'] = result if options.get('legacy') else {**result,'resultType':'complete'}
    print(json.dumps(response),flush=True)
'''


def _config(tmp_path, options):
    script = tmp_path / "peer.py"
    script.write_text(_SERVER.replace("OPTIONS", repr(json.dumps(options))))
    transport = {
        "type": "stdio", "command": sys.executable, "args": [str(script)],
    }
    if options.get("echo_secret"):
        transport["secret_env"] = {
            "MCP_TOKEN": {"source":"environment", "name":"MCP_RPC_TEST_SECRET"},
        }
    config = _parse_server("probe", {
        "required": True,
        "effect_policy": {"default_effect": "READ_ONLY"},
        "transport": transport,
    })
    return config


@asynccontextmanager
async def _runtime(tmp_path, **options):
    config = _config(tmp_path, options)
    supervisor = McpHostSupervisor(
        session_id="session:rpc-review", workspace_root=tmp_path,
        configs=(config,), credential_boundary=ProcessCredentialBoundary(),
    )
    runtime = None
    try:
        await supervisor.start()
        runtime = supervisor.install_pending_at_safe_point()
        assert runtime is not None
        yield supervisor, runtime
    finally:
        if runtime is not None:
            runtime.release()
        await supervisor.aclose()


@pytest.mark.parametrize("method,field", [
    ("tools/list", "tools"),
    ("resources/list", "resources"),
    ("resources/templates/list", "resource_templates"),
    ("prompts/list", "prompts"),
])
def test_empty_opaque_cursor_reaches_all_discovery_methods(tmp_path, method, field):
    async def exercise():
        async with _runtime(tmp_path, pagination_method=method) as (_supervisor, runtime):
            snapshot = runtime.candidates["probe"].discovery_snapshot
            assert len(getattr(snapshot, field)) == 2
            requests = json.loads((tmp_path / "requests.json").read_text())
            pages = [request for request in requests if request["method"] == method]
            assert len(pages) == 2
            assert "cursor" not in (pages[0].get("params") or {})
            assert pages[1]["params"]["cursor"] == ""

    asyncio.run(exercise())


def test_remote_error_data_retains_diagnostics_and_scrubs_actual_secret(tmp_path, monkeypatch):
    secret = "synthetic-rpc-test-secret"
    monkeypatch.setenv("MCP_RPC_TEST_SECRET", secret)

    async def exercise():
        async with _runtime(
            tmp_path, error_method="tools/call", error_code=-32602, echo_secret=True,
        ) as (_supervisor, runtime):
            executor = next(iter(runtime.executors.values()))
            permit = executor.admit(
                session_id="session:rpc-review", scope_kind=ModelInputScopeKind.ROOT,
                turn_id="turn:rpc-review",
                tool_call_id="call:rpc-review:secret",
            )
            permit.mark_attempt_accepted()
            result = await executor.invoke(permit, {})
            body = json.loads(result.content)
            assert body["error"] == "MCP_REMOTE_ERROR"
            assert secret.encode() not in result.content
            assert body["remote_error"]["data"] == {
                "reason":"server rejected request", "retryHint":"fix parameters",
                "credential":"[credential removed]",
            }

    asyncio.run(exercise())


@pytest.mark.parametrize("error_code", [-32602, -32042])
@pytest.mark.parametrize("method,tool_name,arguments", [
    ("tools/call", None, {}),
    ("resources/read", "read_mcp_resource", {"target":{"kind":"MCP_RESOURCE","server_id":"probe", "uri":"fixture://first"}}),
    ("prompts/get", "get_mcp_prompt", {"target":{"kind":"MCP_PROMPT","server_id":"probe", "name":"first"}}),
])
def test_remote_rpc_error_keeps_slot_and_allows_next_operation(
    tmp_path, error_code, method, tool_name, arguments,
):
    async def exercise():
        async with _runtime(tmp_path, error_method=method, error_code=error_code) as (supervisor, runtime):
            slot = runtime.slot_lease_by_server["probe"]._slot
            client = slot.client
            for index in range(2):
                identity = {
                    "session_id": "session:rpc-review",
                    "scope_kind": ModelInputScopeKind.ROOT,
                    "turn_id": "turn:rpc-review",
                    "tool_call_id": f"call:rpc-review:{index}",
                }
                if tool_name is None:
                    executor = next(iter(runtime.executors.values()))
                    permit = executor.admit(**identity)
                    permit.mark_attempt_accepted()
                    result = await executor.invoke(permit, arguments)
                else:
                    permit = runtime.admit_standard_operation(
                        tool_name=tool_name, arguments=arguments,
                        descriptor_fingerprint="sha256:standard-read", **identity,
                    )
                    result = await runtime.invoke_standard(
                        tool_name=tool_name, arguments=arguments, permit=permit,
                        scope_kind=ModelInputScopeKind.ROOT,
                    )
                body = json.loads(result.content)
                if index == 0:
                    assert result.state == "SYSTEM_ERROR"
                    assert body["error"] == "MCP_REMOTE_ERROR"
                    assert body["remote_error"]["code"] == error_code
                    assert "requested operation rejected" in body["remote_error"]["message"]
                    assert body["remote_error"]["data"] == {
                        "reason":"server rejected request", "retryHint":"fix parameters",
                    }
                    assert body["retry_performed"] is False
                    assert "remote_outcome" not in body
                else:
                    assert result.state == "SUCCESS"
                    assert b"same connection success" in result.content
                assert slot.dispatch_fence_state.value == "OPEN"
                assert slot.client is client
                assert "probe" not in supervisor._retry_tasks
            requests = json.loads((tmp_path / "requests.json").read_text())
            assert sum(request["method"] == method for request in requests) == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("legacy", [False, True])
def test_missing_template_method_keeps_tools_and_resources_available(tmp_path, legacy):
    async def exercise():
        async with _runtime(tmp_path, legacy=legacy, error_method="resources/templates/list", error_code=-32601) as (supervisor, runtime):
            candidate = runtime.candidates["probe"]
            snapshot = candidate.discovery_snapshot
            assert snapshot.protocol_version == ("2025-11-25" if legacy else "2026-07-28")
            assert len(snapshot.tools) == len(snapshot.resources) == len(snapshot.prompts) == 1
            assert snapshot.resource_templates == ()
            assert "probe" not in supervisor._retry_tasks
            result = await candidate.slot_lease._slot.client.session.call_tool("first", {})
            assert result.content[0].text == "same connection success"
            requests = json.loads((tmp_path / "requests.json").read_text())
            assert sum(r["method"] == "resources/templates/list" for r in requests) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("method,code,page", [
    ("resources/templates/list", -32602, 1),
    ("resources/templates/list", -32603, 1),
    ("resources/templates/list", -32001, 1),
    ("resources/templates/list", -32601, 2),
    ("tools/list", -32601, 1),
    ("resources/list", -32601, 1),
    ("prompts/list", -32601, 1),
])
def test_optional_templates_does_not_hide_other_or_partial_discovery_errors(tmp_path, method, code, page, legacy):
    async def exercise():
        config = _config(tmp_path, dict(legacy=legacy, error_method=method, error_code=code,
                         error_on_page=page, pagination_method=method if page == 2 else None))
        async def notify(_):
            pass
        client = BoundedMcpSdkClient(config, workspace_root=tmp_path,
                    notification_callback=notify, credential_boundary=ProcessCredentialBoundary())
        try:
            await client.open()
            with pytest.raises(MCPError) as caught:
                await discover_mcp_catalog(client, config)
            assert caught.value.code == code
            requests = json.loads((tmp_path / "requests.json").read_text())
            observed = [r for r in requests if r["method"] == method]
            assert len(observed) == page
            if page == 2:
                assert observed[-1]["params"]["cursor"] == ""
        finally:
            await client.aclose()
    asyncio.run(exercise())
