import asyncio
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pulsara_agent.capability.descriptor import CapabilityProviderKind
from pulsara_agent.capability.providers.mcp import McpCapabilityProvider, build_mcp_bundle
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.capability.types import CapabilityResolveContext
from pulsara_agent.event import (
    CustomEvent,
    EventContext,
    ReplyEndEvent,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pulsara_agent.host.identity import HostWorkspaceInput, resolve_workspace
from pulsara_agent.host.session import HostSession
from pulsara_agent.message import ToolCallBlock, ToolCallState, ToolResultState
from pulsara_agent.llm import LLMConfig, LLMRuntime, ModelProfile
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.transport import LLMTransport
from pulsara_agent.runtime.agent import AgentRuntime
from pulsara_agent.runtime.approval import ApprovalResolution, ToolApprovalDecision
from pulsara_agent.runtime.plan import McpElicitationResolution, PendingMcpElicitation
from pulsara_agent.runtime.state import LoopStatus
from pulsara_agent.runtime import AgentRuntimeWiring, build_in_memory_runtime_wiring
from pulsara_agent.runtime.mcp import (
    McpDiscoveredTool,
    HttpMcpClientManager,
    McpServerConfig,
    McpServerSnapshot,
    McpServerStatus,
    McpStdioConfig,
    McpStreamableHttpConfig,
    McpToolAnnotations,
    MockMcpClientManager,
    StdioMcpClientManager,
    mangle_mcp_tool_name,
)
from pulsara_agent.runtime.permission import (
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionProfile,
    PolicyPermissionGate,
    TerminalAccess,
)
from pulsara_agent.tools.base import ToolCall


def _tool(name: str = "lookup", *, read_only: bool | None = None) -> McpDiscoveredTool:
    return McpDiscoveredTool(
        server_id="docs",
        name=name,
        description="Lookup docs",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        annotations=McpToolAnnotations(read_only_hint=read_only),
    )


def _snapshot(*tools: McpDiscoveredTool, status: McpServerStatus = McpServerStatus.READY) -> McpServerSnapshot:
    return McpServerSnapshot(
        config=McpServerConfig(
            server_id="docs",
            transport=McpStreamableHttpConfig(url="http://127.0.0.1:8765/mcp"),
            supports_parallel_tool_calls=True,
            tool_timeout_ms=1_000,
        ),
        status=status,
        tools=tools,
        generation=7,
    )


def test_mcp_config_dto_filters_tools_and_statuses() -> None:
    config = McpServerConfig(
        server_id="docs server",
        transport=McpStdioConfig(command="python", args=("-m", "fake_mcp")),
        enabled_tools=("lookup",),
        disabled_tools=("delete",),
    )
    snapshot = McpServerSnapshot(
        config=config,
        status=McpServerStatus.READY,
        tools=(
            McpDiscoveredTool(server_id="docs_server", name="lookup", description="", input_schema={}),
            McpDiscoveredTool(server_id="docs_server", name="delete", description="", input_schema={}),
            McpDiscoveredTool(server_id="docs_server", name="other", description="", input_schema={}),
        ),
    )

    assert config.server_id == "docs_server"
    assert config.transport_kind.value == "stdio"
    assert [tool.name for tool in snapshot.tools] == ["lookup"]


def test_mcp_model_tool_name_is_stable_bounded_and_hashed() -> None:
    short = mangle_mcp_tool_name("docs", "lookup")
    long_a = mangle_mcp_tool_name("very-long-server-name-" * 4, "very-long-tool-name-" * 4)
    long_b = mangle_mcp_tool_name("very-long-server-name-" * 4, "very-long-tool-name-" * 4)

    assert short == "mcp__docs__lookup"
    assert long_a == long_b
    assert len(long_a) <= 64
    assert long_a.startswith("mcp__")


def test_mcp_bundle_builds_descriptor_and_execution_binding_from_same_snapshot() -> None:
    manager = MockMcpClientManager((_snapshot(_tool(read_only=True)),), handlers={("docs", "lookup"): lambda args: args})
    bundle = build_mcp_bundle(manager)

    assert [descriptor.name for descriptor in bundle.descriptors] == ["mcp__docs__lookup"]
    assert [tool.name for tool in bundle.tools] == ["mcp__docs__lookup"]
    descriptor = bundle.descriptors[0]
    assert descriptor.provider_kind is CapabilityProviderKind.MCP
    assert descriptor.provider_id == "docs"
    assert descriptor.is_read_only is True
    assert descriptor.is_concurrency_safe is True
    assert descriptor.metadata["original_tool_name"] == "lookup"
    assert bundle.manager is manager
    assert bundle.generation == 7


def test_mcp_provider_exposure_uses_bundle_descriptors_and_registry_bindings(tmp_path: Path) -> None:
    manager = MockMcpClientManager((_snapshot(_tool(read_only=True)),), handlers={("docs", "lookup"): lambda args: args})
    bundle = build_mcp_bundle(manager)
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        wiring = build_in_memory_runtime_wiring(tmp_path, mcp_bundle=bundle)
    executor = wiring.runtime_session.create_tool_executor()
    runtime = CapabilityRuntime.with_default_providers(McpCapabilityProvider(bundle))

    exposure = runtime.resolve_for_turn(
        CapabilityResolveContext(
            workspace_root=tmp_path,
            workspace_kind="transient",
            memory_domain=None,
            available_tool_names=frozenset(executor.registry.names()),
            user_input="",
        ),
        tool_registry=executor.registry,
    )

    assert "mcp__docs__lookup" in exposure.callable_names
    assert [spec.name for spec in exposure.direct_tool_specs if spec.name.startswith("mcp__")] == [
        "mcp__docs__lookup"
    ]


def test_mcp_mock_adapter_executes_through_tool_executor(tmp_path: Path) -> None:
    manager = MockMcpClientManager(
        (_snapshot(_tool(read_only=True)),),
        handlers={("docs", "lookup"): lambda args: {"echo": args["query"]}},
    )
    bundle = build_mcp_bundle(manager)
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        wiring = build_in_memory_runtime_wiring(tmp_path, mcp_bundle=bundle)
    executor = wiring.runtime_session.create_tool_executor()

    result = asyncio.run(
        executor.execute_async(
            ToolCall(id="call:mcp", name="mcp__docs__lookup", arguments={"query": "pulsara"}),
            event_context=EventContext(run_id="run:1", turn_id="turn:1", reply_id="reply:1"),
            descriptor=bundle.descriptors[0],
        )
    )

    assert result.status is ToolResultState.SUCCESS
    assert '"echo": "pulsara"' in result.output
    assert manager.calls == [("docs", "lookup", {"query": "pulsara"})]


def test_large_mcp_result_uses_tool_artifact_service(tmp_path: Path) -> None:
    manager = MockMcpClientManager(
        (_snapshot(_tool(read_only=True)),),
        handlers={("docs", "lookup"): lambda args: "x" * 9_000},
    )
    bundle = build_mcp_bundle(manager)
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        wiring = build_in_memory_runtime_wiring(tmp_path, mcp_bundle=bundle)
    executor = wiring.runtime_session.create_tool_executor()

    result = asyncio.run(
        executor.execute_async(
            ToolCall(id="call:mcp-large", name="mcp__docs__lookup", arguments={"query": "big"}),
            event_context=EventContext(run_id="run:large", turn_id="turn:large", reply_id="reply:large"),
            descriptor=bundle.descriptors[0],
        )
    )

    assert result.status is ToolResultState.SUCCESS
    assert wiring.runtime_session.tool_result_artifacts.records
    assert len(result.output) == 9_000
    record = next(iter(wiring.runtime_session.tool_result_artifacts.records.values()))
    assert record.metadata["preview"]["preview_policy"] == "full"
    assert record.metadata["preview"]["original_chars"] == 9_000


def test_mcp_tools_fail_closed_under_read_only_profile(tmp_path: Path) -> None:
    manager = MockMcpClientManager((_snapshot(_tool(read_only=True)),), handlers={("docs", "lookup"): lambda args: args})
    bundle = build_mcp_bundle(manager)
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        wiring = build_in_memory_runtime_wiring(tmp_path, mcp_bundle=bundle)
    exposure = CapabilityRuntime.with_default_providers(McpCapabilityProvider(bundle)).resolve_for_turn(
        CapabilityResolveContext(
            workspace_root=tmp_path,
            workspace_kind="transient",
            memory_domain=None,
            available_tool_names=frozenset(wiring.runtime_session.create_tool_executor().registry.names()),
            user_input="",
        ),
        tool_registry=wiring.runtime_session.create_tool_executor().registry,
    )
    gate = PolicyPermissionGate(
        EffectivePermissionPolicy(
            profile=PermissionProfile.READ_ONLY,
            approval=ApprovalPolicy.ON_REQUEST,
            terminal=TerminalAccess.OFF,
        ),
        inner=_AllowAll(),
    )

    decision = asyncio.run(
        gate.evaluate(
            [ToolCall(id="call:mcp", name="mcp__docs__lookup", arguments={"query": "x"})],
            exposure=exposure,
        )
    )

    assert decision.kind.value == "deny"
    assert "not allowed by permission policy" in (decision.reason or "")


def test_mcp_unready_snapshot_is_hidden_with_diagnostic(tmp_path: Path) -> None:
    manager = MockMcpClientManager((_snapshot(_tool(), status=McpServerStatus.NEEDS_AUTH),))
    bundle = build_mcp_bundle(manager)
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        wiring = build_in_memory_runtime_wiring(tmp_path, mcp_bundle=bundle)
    exposure = CapabilityRuntime.with_default_providers(McpCapabilityProvider(bundle)).resolve_for_turn(
        CapabilityResolveContext(
            workspace_root=tmp_path,
            workspace_kind="transient",
            memory_domain=None,
            available_tool_names=frozenset(wiring.runtime_session.create_tool_executor().registry.names()),
            user_input="",
        ),
        tool_registry=wiring.runtime_session.create_tool_executor().registry,
    )

    assert not any(name.startswith("mcp__") for name in exposure.direct_names)
    assert [diagnostic.code for diagnostic in exposure.diagnostics if diagnostic.code.startswith("mcp_")] == [
        "mcp_server_needs_auth"
    ]


def test_host_session_close_closes_mcp_manager_once(tmp_path: Path) -> None:
    manager = MockMcpClientManager((_snapshot(_tool()),), handlers={("docs", "lookup"): lambda args: args})
    bundle = build_mcp_bundle(manager)
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        runtime_wiring = build_in_memory_runtime_wiring(tmp_path, mcp_bundle=bundle)
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_kind="transient", workspace_root=tmp_path)),
        wiring=AgentRuntimeWiring(
            agent_runtime=_CloseOnlyAgentRuntime(runtime_wiring.runtime_session),
            runtime_wiring=runtime_wiring,
        ),
    )

    asyncio.run(session.aclose())
    asyncio.run(session.aclose())

    assert manager.close_count == 1
    assert manager.cancel_count == 1


def test_host_session_captures_and_resolves_mcp_elicitation(tmp_path: Path) -> None:
    manager = MockMcpClientManager((_snapshot(_tool()),), handlers={("docs", "lookup"): lambda args: args})
    bundle = build_mcp_bundle(manager)
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        runtime_wiring = build_in_memory_runtime_wiring(tmp_path, mcp_bundle=bundle)
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:mcp",
                        "name": "mcp__docs__lookup",
                        "arguments": json.dumps(
                            {
                                "__mcp_elicitation__": {
                                    "request_id": "request-host",
                                    "prompt": "Need a token",
                                }
                            }
                        ),
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    agent = AgentRuntime(
        runtime_session=runtime_wiring.runtime_session,
        llm_runtime=_llm_runtime(transport),
        capability_runtime=CapabilityRuntime.with_default_providers(McpCapabilityProvider(bundle)),
        permission_policy=EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.ALLOW,
        ),
    )
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_kind="transient", workspace_root=tmp_path)),
        wiring=AgentRuntimeWiring(agent_runtime=agent, runtime_wiring=runtime_wiring),
    )

    first = asyncio.run(session.run_turn("call mcp"))

    assert first.status.value == "waiting_user"
    pending = session.get_pending_interaction()
    assert isinstance(pending, PendingMcpElicitation)
    assert pending.tool_call_id == "call:mcp"
    assert pending.server_id == "docs"

    final = asyncio.run(
        session.resolve_mcp_elicitation(
            McpElicitationResolution(
                interaction_id=pending.interaction_id,
                answer={"value": "secret"},
            )
        )
    )

    assert final.status.value == "finished"
    assert session.get_pending_interaction() is None
    assert manager.elicitation_responses == [
        ("docs", "request-host", {"value": "secret", "tool_call_id": "call:mcp"})
    ]


def test_mcp_elicitation_suspends_and_resume_routes_answer_to_manager(tmp_path: Path) -> None:
    manager = MockMcpClientManager((_snapshot(_tool()),), handlers={("docs", "lookup"): lambda args: args})
    bundle = build_mcp_bundle(manager)
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        runtime_wiring = build_in_memory_runtime_wiring(tmp_path, mcp_bundle=bundle)
    agent = AgentRuntime(
        runtime_session=runtime_wiring.runtime_session,
        llm_runtime=_FinalTextRuntime(),
        capability_runtime=CapabilityRuntime.with_default_providers(McpCapabilityProvider(bundle)),
        permission_policy=EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.ALLOW,
        ),
    )
    state = agent.new_state()

    events = asyncio.run(
        _collect(
            agent._stream_parsed_tool_calls(
                state,
                [
                    ToolCall(
                        id="call:mcp",
                        name="mcp__docs__lookup",
                        arguments={
                            "__mcp_elicitation__": {
                                "request_id": "request-1",
                                "prompt": "Token please",
                                "schema": {"type": "object"},
                            }
                        },
                    )
                ],
            )
        )
    )

    assert state.status.value == "waiting_user"
    assert state.pending_interaction_kind == "mcp_elicitation"
    assert any(
        isinstance(event, CustomEvent) and event.name == "tool_execution_suspended"
        for event in events
    )
    pending = PendingMcpElicitation(
        **{
            **state.pending_interaction_payload,
            "kind": "mcp_elicitation",
            "host_session_id": "host:test",
            "runtime_session_id": state.session_id,
            "run_id": state.run_id,
            "turn_id": state.turn_id,
            "reply_id": state.reply_id,
        }
    )

    result = asyncio.run(
        agent.resume_after_mcp_elicitation(
            state,
            McpElicitationResolution(
                interaction_id=pending.interaction_id,
                answer={"value": "secret"},
            ),
        )
    )

    assert result.status.value == "finished"
    assert manager.elicitation_responses == [
        ("docs", "request-1", {"value": "secret", "tool_call_id": "call:mcp"})
    ]
    assert any(message.role == "tool_result" and message.name == "mcp__docs__lookup" for message in state.messages)


def test_approved_pending_mcp_call_reruns_capability_gate_and_fails_closed(tmp_path: Path) -> None:
    manager = MockMcpClientManager((_snapshot(_tool()),), handlers={("docs", "lookup"): lambda args: args})
    bundle = build_mcp_bundle(manager)
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        runtime_wiring = build_in_memory_runtime_wiring(tmp_path, mcp_bundle=bundle)
    agent = AgentRuntime(
        runtime_session=runtime_wiring.runtime_session,
        llm_runtime=_FinalTextRuntime(),
        capability_runtime=CapabilityRuntime(providers=()),
        permission_policy=EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.ALLOW,
        ),
    )
    state = agent.new_state()
    state.status = LoopStatus.WAITING_USER
    state.stop_reason = "waiting_user"
    state.pending_tool_calls = [
        ToolCallBlock(
            id="call:mcp",
            name="mcp__docs__lookup",
            input=json.dumps({"query": "stale"}),
            state=ToolCallState.ASKING,
        )
    ]

    result = asyncio.run(
        agent.resume_after_approval(
            state,
            ApprovalResolution(
                approval_id="approval:test",
                decisions=(ToolApprovalDecision(tool_call_id="call:mcp", confirmed=True),),
            ),
        )
    )

    assert result.status.value == "finished"
    assert manager.calls == []
    tool_messages = [message for message in state.messages if message.role == "tool_result"]
    assert tool_messages
    assert "capability_descriptor_missing" in tool_messages[0].content[0].output[0].text


def test_streamable_http_mcp_manager_discovers_and_calls_fake_server() -> None:
    server, url, seen = _start_http_mcp_fixture()
    try:
        config = McpServerConfig(
            server_id="http_docs",
            transport=McpStreamableHttpConfig(url=url),
            tool_timeout_ms=1_000,
        )
        manager = asyncio.run(HttpMcpClientManager.discover((config,)))
        try:
            assert manager.snapshots[0].status is McpServerStatus.READY
            assert [tool.name for tool in manager.snapshots[0].tools] == ["lookup"]

            result = asyncio.run(manager.call_tool("http_docs", "lookup", {"query": "mcp"}, timeout_ms=1_000))

            assert result == "lookup:mcp"
            assert [request["method"] for request in seen] == ["tools/list", "tools/call"]
        finally:
            asyncio.run(manager.aclose())
    finally:
        server.shutdown()
        server.server_close()


def test_streamable_http_mcp_manager_reports_missing_bearer_token() -> None:
    config = McpServerConfig(
        server_id="secure",
        transport=McpStreamableHttpConfig(
            url="http://127.0.0.1:9/mcp",
            bearer_token_env_var="PULSARA_TEST_MISSING_MCP_TOKEN",
        ),
    )

    manager = asyncio.run(HttpMcpClientManager.discover((config,)))
    try:
        assert manager.snapshots[0].status is McpServerStatus.NEEDS_AUTH
        assert "missing bearer token" in (manager.snapshots[0].message or "")
    finally:
        asyncio.run(manager.aclose())


def test_stdio_mcp_manager_discovers_calls_and_closes_fixture(tmp_path: Path) -> None:
    fixture = tmp_path / "stdio_mcp_fixture.py"
    fixture.write_text(
        """
import json
import sys

def read_message():
    headers = []
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b"\\r\\n":
            break
        headers.append(line.decode("ascii").strip())
    length = None
    for header in headers:
        if header.lower().startswith("content-length:"):
            length = int(header.split(":", 1)[1].strip())
    if length is None:
        raise RuntimeError("missing content length")
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))

def write_message(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode("ascii") + b"\\r\\n\\r\\n" + body)
    sys.stdout.buffer.flush()

while True:
    request = read_message()
    if request is None:
        break
    method = request.get("method")
    params = request.get("params") or {}
    if method == "tools/list":
        result = {"tools": [{"name": "lookup", "description": "Lookup", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "stdio:" + str((params.get("arguments") or {}).get("query"))}]}
    elif method == "elicitation/respond":
        result = {"ok": True, "answer": params.get("answer")}
    else:
        result = {"unknown": method}
    write_message({"jsonrpc": "2.0", "id": request.get("id"), "result": result})
""".strip(),
        encoding="utf-8",
    )
    config = McpServerConfig(
        server_id="stdio_docs",
        transport=McpStdioConfig(command="python", args=(str(fixture),), cwd=tmp_path),
        tool_timeout_ms=1_000,
    )

    async def run() -> None:
        manager = await StdioMcpClientManager.start((config,))
        try:
            assert manager.snapshots[0].status is McpServerStatus.READY
            assert [tool.name for tool in manager.snapshots[0].tools] == ["lookup"]

            result = await manager.call_tool("stdio_docs", "lookup", {"query": "mcp"}, timeout_ms=1_000)

            assert result == "stdio:mcp"
        finally:
            await manager.aclose()

    asyncio.run(run())


def _start_http_mcp_fixture():
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            seen.append(payload)
            method = payload.get("method")
            params = payload.get("params") or {}
            if method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "lookup",
                            "description": "Lookup",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                            },
                        }
                    ]
                }
            elif method == "tools/call":
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": "lookup:" + str((params.get("arguments") or {}).get("query")),
                        }
                    ]
                }
            else:
                result = {"ok": True}
            body = json.dumps({"jsonrpc": "2.0", "id": payload.get("id"), "result": result}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/mcp", seen


async def _collect(stream):
    return [event async for event in stream]


class _FinalTextRuntime:
    async def stream(self, *, role, context, event_context, options=None):
        del role, context, options
        yield ReplyStartEvent(**event_context.event_fields(), name="assistant")
        yield TextBlockStartEvent(**event_context.event_fields(), block_id="text:final")
        yield TextBlockDeltaEvent(**event_context.event_fields(), block_id="text:final", delta="done")
        yield TextBlockEndEvent(**event_context.event_fields(), block_id="text:final")
        yield ReplyEndEvent(**event_context.event_fields())


class _ScriptedTransport(LLMTransport):
    api = "scripted"

    def __init__(self, replies: list[dict]) -> None:
        self.replies = replies
        self.contexts: list[LLMContext] = []

    async def stream(
        self,
        *,
        model: ModelProfile,
        context: LLMContext,
        event_context: EventContext,
        options: LLMOptions | None = None,
    ):
        del options
        self.contexts.append(context)
        reply = self.replies.pop(0)
        yield ModelCallStartEvent(
            **event_context.event_fields(),
            model_name=model.id,
            model_role=model.role.value,
            provider=model.provider,
        )
        if "text" in reply:
            yield TextBlockStartEvent(**event_context.event_fields(), block_id="text:scripted")
            yield TextBlockDeltaEvent(**event_context.event_fields(), block_id="text:scripted", delta=reply["text"])
            yield TextBlockEndEvent(**event_context.event_fields(), block_id="text:scripted")
        for call in reply.get("tool_calls", []):
            yield ToolCallStartEvent(
                **event_context.event_fields(),
                tool_call_id=call["id"],
                tool_call_name=call["name"],
            )
            yield ToolCallDeltaEvent(
                **event_context.event_fields(),
                tool_call_id=call["id"],
                delta=call["arguments"],
            )
            yield ToolCallEndEvent(**event_context.event_fields(), tool_call_id=call["id"])
        yield ModelCallEndEvent(**event_context.event_fields())


def _llm_runtime(transport: _ScriptedTransport) -> LLMRuntime:
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        pro_model="pro",
        flash_model="flash",
        api="scripted",
    )
    registry = LLMTransportRegistry()
    registry.register(transport)
    return LLMRuntime(config=config, registry=registry)


class _AllowAll:
    async def evaluate(self, calls):
        from pulsara_agent.runtime.permission import PermissionDecision

        return PermissionDecision.allow()


@dataclass(slots=True)
class _CloseOnlyAgentRuntime:
    runtime_session: object

    def close(self) -> None:
        self.runtime_session.close()
