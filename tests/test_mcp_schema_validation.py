"""Regressions through real SDK sessions and public tool authorization."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import yaml
from jsonschema import ValidationError
from referencing.exceptions import Unresolvable

from pulsara_agent.conversation_kernel.mcp.schema import (
    LOCAL_MCP_SCHEMA_REGISTRY,
    McpSchemaEvaluationFailure,
    mcp_schema_validator,
    validate_mcp_arguments,
)
from pulsara_agent.conversation_kernel.mcp.sdk_facade import SchemaBoundMcpClientSession
from pulsara_agent.mcp_config import load_mcp_server_configs
from tests import test_round6_mcp_production as fixture


@pytest.fixture
def schema_endpoint():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append(self.path)
            body = b'{"type":"string"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/schema+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/private-schema", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _stdio_config(
    tmp_path, input_schema, responses, *, output_schema=None,
    protocol_version="2026-07-28",
):
    """One raw protocol fixture; SDK negotiation, listing and calls stay real."""

    tool = {"name": "schema_probe", "inputSchema": input_schema}
    if output_schema is not None:
        tool["outputSchema"] = output_schema
    script = tmp_path / "schema_server.py"
    calls = tmp_path / "calls.json"
    script.write_text(
        "import json, sys\n"
        f"tool = json.loads({json.dumps(json.dumps(tool))})\n"
        f"responses = json.loads({json.dumps(json.dumps(responses))})\n"
        f"protocol_version = {protocol_version!r}\n"
        "calls = []\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    if 'id' not in request: continue\n"
        "    method = request['method']\n"
        "    if method == 'server/discover':\n"
        "        if protocol_version < '2026-07-28':\n"
        "            print(json.dumps({'jsonrpc':'2.0','id':request['id'],'error':{'code':-32601,'message':'use initialize'}}), flush=True)\n"
        "            continue\n"
        "        result = {'supportedVersions':['2026-07-28'], 'capabilities':{'tools':{}}}\n"
        "    elif method == 'initialize':\n"
        "        result = {'protocolVersion':protocol_version, 'capabilities':{'tools':{}}, 'serverInfo':{'name':'schema-fixture','version':'1'}}\n"
        "    elif method == 'tools/list': result = {'tools':[tool]}\n"
        "    elif method == 'tools/call':\n"
        "        result = responses[len(calls)]\n"
        "        calls.append(request['params'])\n"
        f"        with open({str(calls)!r}, 'w') as stream: json.dump(calls, stream)\n"
        "    else: raise RuntimeError(method)\n"
        "    if protocol_version >= '2026-07-28' and method in ('server/discover', 'tools/list'):\n"
        "        result.update(cacheScope='private', ttlMs=0)\n"
        "    result['resultType'] = 'complete'\n"
        "    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}), flush=True)\n"
    )
    fixture._config(tmp_path)
    config_path = tmp_path / "mcp.yaml"
    document = yaml.safe_load(config_path.read_text())
    entry = document["servers"]["fixture"]
    entry["transport"]["args"] = [str(script)]
    entry["effect_policy"] = {"default_effect": "READ_ONLY"}
    entry["exposure_policy"] = {"invalid_tool_policy": "OMIT_INVALID"}
    config_path.write_text(yaml.safe_dump(document))
    return load_mcp_server_configs(user_config_path=config_path)[0], calls


def _port(tmp_path, supervisor):
    port = fixture.DirectKernelToolPort(
        workspace_root=tmp_path,
        host_owner_id="host:um5edy62izvdyaqfp7sleqd3qm",
        session_id="session:vbyef3afwygcmgc6kb7o4cgo3u",
        live_bus=fixture.LiveAgentEventBus(),
        authorization_policy=fixture.DefaultToolDispatchAuthorizationPolicy(),
    )
    port.bind_mcp_supervisor(supervisor)
    fixture._seal_mcp_test_port(port)
    return port


@pytest.mark.parametrize("reference", ["$ref", "$dynamicRef", "$recursiveRef", "base", "hidden"])
@pytest.mark.parametrize("destination", ["http", "file"])
def test_discovery_and_public_authorization_never_retrieve_schema(
    tmp_path, schema_endpoint, reference, destination
):
    endpoint, requests = schema_endpoint
    target = endpoint if destination == "http" else (tmp_path / "private.json").as_uri()
    (tmp_path / "private.json").write_text('{"type":"string"}')
    dialect = (
        "https://json-schema.org/draft/2019-09/schema"
        if reference == "$recursiveRef"
        else "https://json-schema.org/draft/2020-12/schema"
    )
    schema = {"$schema": dialect, "type": "object", "properties": {"text": {}}}
    if reference == "base":
        schema["properties"]["text"] = {"$id": target, "$ref": "#/private"}
    elif reference == "hidden":
        schema["custom"] = {"schema": {"$dynamicRef": target}}
        schema["properties"]["text"] = {"$ref": "#/custom/schema"}
    else:
        schema["properties"]["text"] = {reference: target}
    config, calls = _stdio_config(tmp_path, schema, [])

    async def exercise():
        supervisor = fixture.McpHostSupervisor(
            session_id="session:vbyef3afwygcmgc6kb7o4cgo3u",
            workspace_root=tmp_path, configs=(config,),
        )
        port = _port(tmp_path, supervisor)
        borrow = None
        try:
            await supervisor.start()
            port.prepare_tool_surface_safe_point()
            surface = fixture.prepare_test_direct_tool_surface(port)
            borrow = port.borrow_tool_surface(surface)
            snapshot = port._mcp_current.candidates["fixture"].discovery_snapshot
            assert snapshot.invalid_tool_count == 1
            assert not snapshot.tools
            permission = fixture.build_run_permission_snapshot(
                snapshot_id="permission:xe33u4flpejjy72jmvg5arcvtu",
                requested_mode=fixture.PermissionMode.READ_ONLY,
                effective_mode=fixture.PermissionMode.READ_ONLY,
                admission_source=fixture.RunPermissionAdmissionSource.USER_SUBMISSION,
            )
            authorization = await port.authorize(
                tool_name="mcp__fixture__schema_probe", arguments={"text": "hello"},
                tool_call_id="call:schema", turn_id="turn:fugd2z4wkqsu77svw4oie5ktvy",
                assistant_entry_id="entry:5qidalqvkujk5ykzzzfpwie2qy",
                permission_snapshot=permission, surface_borrow=borrow,
                memory_context=fixture._enabled_memory_context(),
            )
            assert authorization.kind is fixture.KernelToolAuthorizationKind.TOOL_UNAVAILABLE
            assert not calls.exists()
            assert not requests
        finally:
            if borrow is not None:
                borrow.close()
            supervisor.stop_admission()
            close = asyncio.create_task(supervisor.aclose())
            await port.aclose(timeout_seconds=5)
            await close

    asyncio.run(exercise())


@pytest.mark.parametrize("dialect", [
    "http://json-schema.org/draft-07/schema#",
    "https://json-schema.org/draft/2019-09/schema",
    "https://json-schema.org/draft/2020-12/schema",
])
def test_document_local_refs_and_embedded_ids_keep_validation(dialect):
    schema = {"$schema": dialect, "$id": "https://schema.invalid/root", "type": "object",
              "definitions": {"text": {"type": "string"}},
              "properties": {"text": {"$ref": "#/definitions/text"},
                             "payload": {"const": {"$dynamicRef": "https://data.invalid/value"}},
                             "nested": {"$id": "child", "type": "object",
                                        "definitions": {"count": {"type": "integer"}},
                                        "properties": {"count": {"$ref": "#/definitions/count"}}}}}
    validator = mcp_schema_validator(schema)
    validator.validate({"text": "yes", "nested": {"count": 1},
                        "payload": {"$dynamicRef": "https://data.invalid/value"}})
    with pytest.raises(ValidationError):
        validator.validate({"text": "yes", "nested": {"count": "wrong"}})


def test_document_local_refs_reach_public_authorization(tmp_path):
    schema = {
        "type": "object",
        "$defs": {"text": {"type": "string"}},
        "properties": {"text": {"$ref": "#/$defs/text"}},
        "required": ["text"],
    }
    config, calls = _stdio_config(tmp_path, schema, [])

    async def exercise():
        supervisor = fixture.McpHostSupervisor(
            session_id="session:vbyef3afwygcmgc6kb7o4cgo3u",
            workspace_root=tmp_path, configs=(config,),
        )
        port = _port(tmp_path, supervisor)
        borrow = None
        try:
            await supervisor.start()
            port.prepare_tool_surface_safe_point()
            surface = fixture.prepare_test_direct_tool_surface(port)
            borrow = port.borrow_tool_surface(surface)
            permission = fixture.build_run_permission_snapshot(
                snapshot_id="permission:xe33u4flpejjy72jmvg5arcvtu",
                requested_mode=fixture.PermissionMode.READ_ONLY,
                effective_mode=fixture.PermissionMode.READ_ONLY,
                admission_source=fixture.RunPermissionAdmissionSource.USER_SUBMISSION,
            )
            for value, expected in (
                (42, fixture.KernelToolAuthorizationKind.INVALID_ARGUMENTS),
                ("hello", fixture.KernelToolAuthorizationKind.ALLOW),
            ):
                authorization = await port.authorize(
                    tool_name="mcp__fixture__schema_probe", arguments={"text": value},
                    tool_call_id=f"call:local:{value}",
                    turn_id="turn:fugd2z4wkqsu77svw4oie5ktvy",
                    assistant_entry_id="entry:5qidalqvkujk5ykzzzfpwie2qy",
                    permission_snapshot=permission, surface_borrow=borrow,
                    memory_context=fixture._enabled_memory_context(),
                )
                assert authorization.kind is expected
            assert not calls.exists()
        finally:
            if borrow is not None:
                borrow.close()
            supervisor.stop_admission()
            close = asyncio.create_task(supervisor.aclose())
            await port.aclose(timeout_seconds=5)
            await close

    asyncio.run(exercise())


def test_validator_registry_itself_cannot_retrieve(schema_endpoint):
    # Exercise the actual validator independently of admission's reference scan.
    from jsonschema import Draft202012Validator

    endpoint, requests = schema_endpoint
    validator = Draft202012Validator(
        {"$dynamicRef": endpoint}, registry=LOCAL_MCP_SCHEMA_REGISTRY
    )
    with pytest.raises(Unresolvable):
        validator.validate("hello")
    assert not requests


@pytest.mark.parametrize("protocol", ["2026-07-28", "2025-11-25"])
@pytest.mark.parametrize("response,output_schema", [
    ({"content": [], "structuredContent": None}, {"type": "null"}),
    ({"content": [], "structuredContent": None}, {"type": ["object", "null"]}),
    ({"content": [], "structuredContent": None}, {"type": "object"}),
    ({"content": []}, {"type": "null"}),
    ({"content": [], "structuredContent": None}, None),
    ({"content": []}, None),
])
def test_real_sdk_distinguishes_explicit_null_from_unset(
    tmp_path, protocol, response, output_schema
):
    config, calls = _stdio_config(
        tmp_path, {"type": "object"}, [response],
        output_schema=output_schema, protocol_version=protocol,
    )

    async def exercise():
        supervisor = fixture.McpHostSupervisor(
            session_id="session:vbyef3afwygcmgc6kb7o4cgo3u",
            workspace_root=tmp_path, configs=(config,),
        )
        runtime = None
        try:
            if (
                protocol == "2025-11-25"
                and output_schema is not None
                and output_schema["type"] != "object"
            ):
                # The negotiated legacy SDK rejects non-object output schemas
                # during listing, before any tool invocation is possible.
                with pytest.raises(RuntimeError, match="required MCP server failed"):
                    await supervisor.start()
                assert not calls.exists()
                return
            await supervisor.start()
            runtime = supervisor.install_pending_at_safe_point()
            executor = next(iter(runtime.executors.values()))
            permit = executor.admit(
                session_id="session:vbyef3afwygcmgc6kb7o4cgo3u",
                scope_kind=fixture.ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id="turn:fugd2z4wkqsu77svw4oie5ktvy", tool_call_id="call:null",
            )
            permit.mark_attempt_accepted()
            result = await executor.invoke(permit, {})
            body = json.loads(result.content)
            success = output_schema is None or (
                protocol == "2026-07-28"
                and "structuredContent" in response
                and output_schema["type"] != "object"
            )
            assert result.state == ("SUCCESS" if success else "SYSTEM_ERROR")
            if success:
                assert ("structuredContent" in body) == ("structuredContent" in response)
                if "structuredContent" in body:
                    assert body["structuredContent"] is None
            else:
                assert body["error"] == "MCP_OUTPUT_SCHEMA_MISMATCH"
                assert "remote_outcome" not in body
            assert executor.lease._slot.dispatch_fence_state.value == "OPEN"
            assert len(json.loads(calls.read_text())) == 1
        finally:
            if runtime is not None:
                runtime.release()
            await supervisor.aclose()

    asyncio.run(exercise())


def test_recursive_schema_evaluation_is_typed_without_rejecting_normal_recursion():
    with pytest.raises(McpSchemaEvaluationFailure, match="MCP_SCHEMA_EVALUATION_FAILED"):
        validate_mcp_arguments({"type": "object", "allOf": [{"$ref": "#"}]}, {})
    schema = {
        "type": "object",
        "properties": {"next": {"$ref": "#"}, "value": {"type": "integer"}},
    }
    validate_mcp_arguments(schema, {"value": 1, "next": {"value": 2}})
    with pytest.raises(ValidationError):
        validate_mcp_arguments(schema, {"next": {"value": "wrong"}})


def test_recursive_evaluation_stays_local_in_public_prepare_and_authorize(tmp_path):
    # The conditional allows a later ordinary call using the same installed
    # schema. The recursive branch is evaluated without advancing the instance.
    schema = {
        "type": "object",
        "properties": {"recurse": {"type": "boolean"}},
        "if": {"properties": {"recurse": {"const": True}}, "required": ["recurse"]},
        "then": {"$ref": "#"},
    }
    config, calls = _stdio_config(tmp_path, schema, [])

    async def exercise():
        supervisor = fixture.McpHostSupervisor(
            session_id="session:vbyef3afwygcmgc6kb7o4cgo3u",
            workspace_root=tmp_path, configs=(config,),
        )
        port = _port(tmp_path, supervisor)
        borrow = None
        try:
            await supervisor.start()
            port.prepare_tool_surface_safe_point()
            surface = fixture.prepare_test_direct_tool_surface(port)
            borrow = port.borrow_tool_surface(surface)
            snapshot = port._mcp_current.candidates["fixture"].discovery_snapshot
            assert snapshot.invalid_tool_count == 0
            assert len(snapshot.tools) == 1
            permission = fixture.build_run_permission_snapshot(
                snapshot_id="permission:xe33u4flpejjy72jmvg5arcvtu",
                requested_mode=fixture.PermissionMode.READ_ONLY,
                effective_mode=fixture.PermissionMode.READ_ONLY,
                admission_source=fixture.RunPermissionAdmissionSource.USER_SUBMISSION,
            )
            for recurse in (True, False):
                arguments = {"recurse": recurse}
                prepared = port.prepare_resolved_invocation(
                    tool_name="mcp__fixture__schema_probe", arguments=arguments,
                    surface_borrow=borrow,
                )
                authorization = await port.authorize(
                    tool_name="mcp__fixture__schema_probe", arguments=arguments,
                    tool_call_id=f"call:recursive:{recurse}",
                    turn_id="turn:fugd2z4wkqsu77svw4oie5ktvy",
                    assistant_entry_id="entry:5qidalqvkujk5ykzzzfpwie2qy",
                    permission_snapshot=permission, surface_borrow=borrow,
                    memory_context=fixture._enabled_memory_context(),
                )
                if recurse:
                    assert prepared.authorization.kind is fixture.KernelToolAuthorizationKind.TOOL_UNAVAILABLE
                    assert authorization.kind is fixture.KernelToolAuthorizationKind.TOOL_UNAVAILABLE
                    assert "MCP_SCHEMA_EVALUATION_FAILED" in prepared.authorization.public_message
                    assert "MCP_SCHEMA_EVALUATION_FAILED" in authorization.public_message
                else:
                    assert prepared.canonical_tool_name == "mcp__fixture__schema_probe"
                    assert authorization.kind is fixture.KernelToolAuthorizationKind.ALLOW
            assert not calls.exists()
        finally:
            if borrow is not None:
                borrow.close()
            supervisor.stop_admission()
            close = asyncio.create_task(supervisor.aclose())
            await port.aclose(timeout_seconds=5)
            await close

    asyncio.run(exercise())


@pytest.mark.parametrize("response,state", [
    ({"content": [{"type": "text", "text": "application denied"}], "isError": True}, "APPLICATION_ERROR"),
    ({"content": [], "structuredContent": {"count": "not-an-integer"}}, "SYSTEM_ERROR"),
    ({"content": []}, "SYSTEM_ERROR"),
    ({"content": [], "structuredContent": {"count": 1}}, "SUCCESS"),
])
def test_real_sdk_output_boundary_preserves_response_and_connection(tmp_path, response, state):
    success = {"content": [], "structuredContent": {"count": 2}}
    output = {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]}
    config, calls = _stdio_config(tmp_path, {"type": "object"}, [response, success], output_schema=output)

    async def exercise():
        supervisor = fixture.McpHostSupervisor(
            session_id="session:vbyef3afwygcmgc6kb7o4cgo3u",
            workspace_root=tmp_path, configs=(config,),
        )
        await supervisor.start()
        runtime = supervisor.install_pending_at_safe_point()
        try:
            executor = next(iter(runtime.executors.values()))
            assert isinstance(executor.lease._slot.client.session, SchemaBoundMcpClientSession)
            for index in range(2):
                permit = executor.admit(
                    session_id="session:vbyef3afwygcmgc6kb7o4cgo3u",
                    scope_kind=fixture.ModelInputScopeKind.ROOT, scope_subagent_task_id=None,
                    turn_id="turn:fugd2z4wkqsu77svw4oie5ktvy", tool_call_id=f"call:schema:{index}",
                )
                permit.mark_attempt_accepted()
                result = await executor.invoke(permit, {})
                assert result.state == (state if index == 0 else "SUCCESS")
                body = json.loads(result.content)
                if index == 0 and state == "APPLICATION_ERROR":
                    assert body["isError"] is True
                    assert body["content"][0]["text"] == "application denied"
                elif index == 0 and state == "SYSTEM_ERROR":
                    assert body["error"] == "MCP_OUTPUT_SCHEMA_MISMATCH"
                    assert "remote_outcome" not in body
                    assert body["retry_performed"] is False
                assert executor.lease._slot.dispatch_fence_state.value == "OPEN"
                assert "fixture" not in supervisor._retry_tasks
            assert len(json.loads(calls.read_text())) == 2
        finally:
            runtime.release()
            await supervisor.aclose()

    asyncio.run(exercise())
