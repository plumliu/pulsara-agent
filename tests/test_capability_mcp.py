import asyncio
import base64
import hashlib
import json
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import mcp_types as sdk_types

from pulsara_agent.capability.descriptor import (
    CapabilityAdvertisePolicy,
    CapabilityAvailability,
    CapabilityDescriptor,
    CapabilityProviderKind,
    CapabilityProvenance,
)
from pulsara_agent.capability.provider import CapabilityProviderOutput
from pulsara_agent.capability.providers.mcp import McpCapabilityProvider, build_mcp_bundle
from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.capability.types import CapabilityResolveContext
from pulsara_agent.event import (
    CapabilityGateDecisionEvent,
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
from pulsara_agent.host import session as host_session_module
from pulsara_agent.host.core import HostCore
from pulsara_agent.host.session import HostSession
from pulsara_agent.message import ToolCallBlock, ToolCallState, ToolResultState
from pulsara_agent.llm import LLMConfig, LLMRuntime, ModelProfile
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.llm.transport import LLMTransport
from pulsara_agent.runtime.agent import AgentRuntime
from pulsara_agent.runtime.approval import ApprovalResolution, ToolApprovalDecision
from pulsara_agent.runtime.plan import (
    McpElicitationResolution,
    McpInputRequiredInteractionResolution,
    PendingMcpElicitation,
    PendingMcpInputRequired,
)
from pulsara_agent.runtime.state import LoopStatus
from pulsara_agent.runtime import AgentRuntimeWiring, build_in_memory_runtime_wiring
from pulsara_agent.runtime.mcp import (
    McpDiscoveredTool,
    HttpMcpClientManager,
    McpInputRequestDTO,
    McpInputRequired,
    McpInputRequiredResolution,
    McpOriginalRequest,
    McpRequestSourceMethod,
    McpServerConfig,
    McpServerSnapshot,
    McpServerStatus,
    McpStdioConfig,
    McpStreamableHttpConfig,
    McpToolAnnotations,
    MockMcpClientManager,
    SdkMcpClientManager,
    StdioMcpClientManager,
    mangle_mcp_tool_name,
)
from pulsara_agent.runtime.mcp.types import redact_mcp_error_message
from pulsara_agent.runtime.mcp.sdk import (
    _redact_diagnostic,
    _sdk_input_responses,
    mcp_tool_result_from_sdk,
)
from pulsara_agent.runtime.mcp.supervisor import McpServerSupervisor, _config_fingerprint
from pulsara_agent.cli import _format_repl_mcp_startup_notice, _mcp_command
from pulsara_agent.runtime.permission import (
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionProfile,
    PolicyPermissionGate,
    TerminalAccess,
)
from pulsara_agent.tools.base import ToolCall, ToolExecutionResult, ToolExecutionSuspended, ToolRuntimeContext
from pulsara_agent.tools.adapters.mcp import McpCapabilityTool


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


def _snapshot_for_config(
    config: McpServerConfig,
    *tools: McpDiscoveredTool,
    status: McpServerStatus = McpServerStatus.READY,
    generation: int = 7,
) -> McpServerSnapshot:
    return McpServerSnapshot(
        config=config,
        status=status,
        tools=tools,
        generation=generation,
    )


def _mcp_input_required_host_session(
    tmp_path: Path,
) -> tuple[HostSession, MockMcpClientManager, AgentRuntimeWiring, AgentRuntime]:
    def request_input(args: dict[str, object]) -> McpInputRequired:
        return McpInputRequired(
            interaction_id="mcp_input_required:host",
            server_id="docs",
            protocol_version="2026-07-28",
            request_state=None,
            input_requests=(
                McpInputRequestDTO(
                    key="token",
                    method="elicitation/create",
                    params={"message": "Need a token", "mode": "form"},
                ),
            ),
            original_request=McpOriginalRequest(
                source_method=McpRequestSourceMethod.TOOL_CALL,
                tool_name="lookup",
                arguments=dict(args),
            ),
        )

    manager = MockMcpClientManager((_snapshot(_tool()),), handlers={("docs", "lookup"): request_input})
    bundle = build_mcp_bundle(manager)
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        runtime_wiring = build_in_memory_runtime_wiring(tmp_path, mcp_bundle=bundle)
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:mcp-input",
                        "name": "mcp__docs__lookup",
                        "arguments": json.dumps({"query": "pulsara"}),
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
    return session, manager, runtime_wiring, agent


@dataclass(slots=True)
class _LegacyMcpElicitationFixtureTool:
    name: str = "legacy_mcp_elicitation_fixture"
    description: str = "Test-only legacy MCP elicitation fixture."
    parameters: dict[str, object] = field(default_factory=lambda: {"type": "object", "properties": {}})
    is_read_only: bool = False
    is_concurrency_safe: bool = False
    responses: list[tuple[str, str, dict[str, object]]] = field(default_factory=list)

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionSuspended:
        del runtime_context
        request_id = str(call.arguments.get("request_id") or "request-1")
        return ToolExecutionSuspended(
            tool_call_id=call.id,
            tool_name=call.name,
            interaction_kind="mcp_elicitation",
            payload={
                "interaction_id": f"mcp_elicitation:{request_id}",
                "tool_call_id": call.id,
                "tool_name": call.name,
                "server_id": "docs",
                "request_id": request_id,
                "prompt": str(call.arguments.get("prompt") or "Token please"),
                "schema": dict(call.arguments.get("schema") or {}),
            },
        )

    async def resume_elicitation(
        self,
        *,
        request_id: str,
        answer: dict[str, object],
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        del runtime_context
        self.responses.append(("docs", request_id, dict(answer)))
        return ToolExecutionResult(
            call_id=str(answer["tool_call_id"]),
            tool_name=self.name,
            status=ToolResultState.SUCCESS,
            output=json.dumps({"elicitation_response": answer, "request_id": request_id}, sort_keys=True),
        )


@dataclass(frozen=True, slots=True)
class _FixtureCapabilityProvider:
    tool_name: str
    provider_id: str = "fixture"

    def resolve(self, context: CapabilityResolveContext, *, bound_tool_names: frozenset[str]) -> CapabilityProviderOutput:
        del context, bound_tool_names
        return CapabilityProviderOutput(
            descriptors=(
                CapabilityDescriptor(
                    id=f"fixture:{self.tool_name}",
                    name=self.tool_name,
                    description="Test-only fixture capability.",
                    input_schema={"type": "object", "properties": {}},
                    namespace="fixture",
                    provider_kind=CapabilityProviderKind.MCP,
                    provider_id="fixture",
                    is_model_callable=True,
                    is_read_only=False,
                    is_concurrency_safe=False,
                    is_destructive=False,
                    is_open_world=False,
                    permission_category="mcp",
                    advertise_policy=CapabilityAdvertisePolicy.DIRECT,
                    availability=CapabilityAvailability.AVAILABLE,
                    provenance=CapabilityProvenance(
                        provider_kind=CapabilityProviderKind.MCP,
                        provider_id="fixture",
                        source="test",
                    ),
                ),
            )
        )


def _write_sdk_stdio_fixture(tmp_path: Path) -> Path:
    fixture = tmp_path / "sdk_mcp_server.py"
    fixture.write_text(
        """
import asyncio
from mcp.server.mcpserver import MCPServer

server = MCPServer(name="pulsara-test-sdk", version="1.0")

@server.tool(description="Lookup docs")
def lookup(query: str) -> str:
    return "lookup:" + query

@server.resource("docs://status", name="status", description="Status resource", mime_type="text/plain")
def status() -> str:
    return "status:ok"

@server.prompt(description="Greeting prompt")
def greet(name: str) -> str:
    return "Say hello to " + name

if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
""".strip(),
        encoding="utf-8",
    )
    return fixture


def _write_sdk_input_required_fixture(tmp_path: Path) -> Path:
    fixture = tmp_path / "sdk_mcp_input_required_server.py"
    fixture.write_text(
        """
import asyncio
import mcp_types as types
from mcp.server.mcpserver import MCPServer

server = MCPServer(name="pulsara-test-input-required", version="1.0")

@server.tool(description="Needs interactive token")
def needs_token(query: str):
    return types.InputRequiredResult(
        inputRequests={
            "token": types.ElicitRequest(
                params=types.ElicitRequestFormParams(
                    message="Need token for " + query,
                    requestedSchema={
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                )
            )
        },
        requestState=None,
    )

if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
""".strip(),
        encoding="utf-8",
    )
    return fixture


def _write_sdk_slow_fixture(tmp_path: Path) -> Path:
    fixture = tmp_path / "sdk_mcp_slow_server.py"
    fixture.write_text(
        """
import asyncio
from mcp.server.mcpserver import MCPServer

server = MCPServer(name="pulsara-test-slow", version="1.0")

@server.tool(description="Slow tool")
async def slow(delay: float) -> str:
    await asyncio.sleep(delay)
    return "done"

if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
""".strip(),
        encoding="utf-8",
    )
    return fixture


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
    server_id = "very-long-server-name-" * 4
    tool_name = "very-long-tool-name-" * 4
    long_a = mangle_mcp_tool_name(server_id, tool_name)
    long_b = mangle_mcp_tool_name(server_id, tool_name)
    digest = hashlib.sha256(f"{server_id}\0{tool_name}".encode("utf-8")).hexdigest()[:10]

    assert short == "mcp__docs__lookup"
    assert long_a == long_b
    assert len(long_a) <= 64
    assert long_a.startswith("mcp__")
    assert long_a.endswith(f"__{digest}")


def test_mcp_input_required_payload_is_pulsara_owned_and_nullable_state() -> None:
    pending = McpInputRequired(
        interaction_id="mcp_input_required:test",
        server_id="docs",
        protocol_version="2026-07-28",
        request_state=None,
        input_requests=(
            McpInputRequestDTO(
                key="token",
                method="elicitation/create",
                params={"message": "Need token", "mode": "form"},
            ),
        ),
        original_request=McpOriginalRequest(
            source_method=McpRequestSourceMethod.TOOL_CALL,
            tool_name="lookup",
            arguments={"query": "x"},
        ),
        round_count=1,
    )

    payload = pending.to_payload()

    assert payload["request_state"] is None
    assert payload["input_requests"] == [
        {
            "key": "token",
            "method": "elicitation/create",
            "params": {"message": "Need token", "mode": "form"},
        }
    ]
    assert payload["original_request"] == {
        "source_method": "tools/call",
        "tool_name": "lookup",
        "arguments": {"query": "x"},
    }


def test_sdk_mcp_manager_discovers_calls_resources_prompts_and_closes(tmp_path: Path) -> None:
    fixture = _write_sdk_stdio_fixture(tmp_path)
    config = McpServerConfig(
        server_id="sdk_docs",
        transport=McpStdioConfig(command=sys.executable, args=(str(fixture),), cwd=tmp_path),
        startup_timeout_ms=3_000,
        tool_timeout_ms=3_000,
    )

    async def run() -> None:
        manager = await SdkMcpClientManager.start((config,))
        try:
            snapshot = manager.snapshots[0]
            assert snapshot.status is McpServerStatus.READY
            assert snapshot.protocol_version == "2026-07-28"
            assert [tool.name for tool in snapshot.tools] == ["lookup"]
            assert [resource.uri for resource in snapshot.resources] == ["docs://status"]
            assert [prompt.name for prompt in snapshot.prompts] == ["greet"]

            tool_result = await manager.call_tool("sdk_docs", "lookup", {"query": "pulsara"}, timeout_ms=3_000)
            assert tool_result.output.startswith("lookup:pulsara")
            assert tool_result.metadata["mcp_result_type"] == "CallToolResult"

            resource_result = await manager.read_resource("sdk_docs", "docs://status", timeout_ms=3_000)
            assert "status:ok" in resource_result.output
            assert resource_result.artifacts

            prompt_result = await manager.get_prompt("sdk_docs", "greet", {"name": "Pulsara"}, timeout_ms=3_000)
            assert "Say hello to Pulsara" in prompt_result.output
        finally:
            await manager.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_sdk_mcp_manager_close_suppresses_internal_cancel_scope_without_poisoning_caller() -> None:
    class CancelOnExitClient:
        exit_called = False

        async def __aexit__(self, exc_type, exc, tb):
            self.exit_called = True
            raise asyncio.CancelledError("SDK internal close cancellation")

    async def run() -> None:
        client = CancelOnExitClient()
        manager = SdkMcpClientManager(
            _snapshots=(),
            _connections={
                "docs": SimpleNamespace(
                    client=client,
                    http_client=None,
                )
            },
        )

        await manager.aclose(timeout_seconds=1)
        await asyncio.sleep(0)

        assert manager._connections == {}
        assert asyncio.current_task() is not None
        assert asyncio.current_task().cancelling() == 0

    asyncio.run(run())


def test_mcp_supervisor_close_suppresses_manager_cancel_scope() -> None:
    class CancelCloseManager:
        snapshots = ()
        close_count = 0

        async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
            self.close_count += 1
            raise asyncio.CancelledError("manager internal close cancellation")

        def cancel_active(self) -> None:
            pass

    async def run() -> None:
        manager = CancelCloseManager()
        supervisor = McpServerSupervisor()
        supervisor._managers["docs"] = manager  # exercise teardown path directly

        await supervisor.aclose(timeout_seconds=1)
        await asyncio.sleep(0)

        assert manager.close_count == 1
        assert supervisor.manager is None
        assert asyncio.current_task() is not None
        assert asyncio.current_task().cancelling() == 0

    asyncio.run(run())


def test_sdk_mcp_manager_maps_input_required_to_pulsara_dto(tmp_path: Path) -> None:
    fixture = _write_sdk_input_required_fixture(tmp_path)
    config = McpServerConfig(
        server_id="sdk_auth",
        transport=McpStdioConfig(command=sys.executable, args=(str(fixture),), cwd=tmp_path),
        startup_timeout_ms=3_000,
        tool_timeout_ms=3_000,
    )

    async def run() -> None:
        manager = await SdkMcpClientManager.start((config,))
        try:
            snapshot = manager.snapshots[0]
            assert snapshot.status is McpServerStatus.READY
            assert [tool.name for tool in snapshot.tools] == ["needs_token"]

            result = await manager.call_tool("sdk_auth", "needs_token", {"query": "pulsara"}, timeout_ms=3_000)

            assert isinstance(result, McpInputRequired)
            assert result.request_state is None
            assert result.original_request.to_dict() == {
                "source_method": "tools/call",
                "tool_name": "needs_token",
                "arguments": {"query": "pulsara"},
            }
            assert len(result.input_requests) == 1
            request = result.input_requests[0]
            assert request.key == "token"
            assert request.method == "elicitation/create"
            assert request.params["message"] == "Need token for pulsara"
            assert request.params["mode"] == "form"
            assert request.params["requestedSchema"]["required"] == ["value"]
        finally:
            await manager.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_sdk_input_required_resolution_builds_typed_sdk_responses() -> None:
    resolution = McpInputRequiredResolution(
        interaction_id="mcp_input_required:test",
        responses={"token": {"value": "secret"}},
        input_requests=(
            McpInputRequestDTO(
                key="token",
                method="elicitation/create",
                params={"message": "Need token", "mode": "form"},
            ),
        ),
    )

    responses = _sdk_input_responses(resolution)

    assert responses is not None
    assert sdk_types.ElicitResult.model_validate(responses["token"]).content == {"value": "secret"}


def test_mcp_redaction_covers_url_userinfo_query_headers_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_SECRET_TOKEN", "env-secret")
    config = McpServerConfig(
        server_id="secure",
        transport=McpStreamableHttpConfig(
            url="https://user:pass@example.test/mcp?token=url-secret#frag",
            headers={"X-Api-Key": "header-secret"},
            env_headers={"Authorization": "MCP_SECRET_TOKEN"},
        ),
    )

    redacted = _redact_diagnostic(
        "failed https://user:pass@example.test/mcp?token=url-secret#frag header-secret env-secret",
        config,
    )

    assert "user:pass" not in redacted
    assert "url-secret" not in redacted
    assert "header-secret" not in redacted
    assert "env-secret" not in redacted
    assert "<redacted-userinfo>@example.test" in redacted


def test_mcp_runtime_error_redaction_covers_runtime_exception_strings() -> None:
    redacted = redact_mcp_error_message(
        "failed https://user:pass@example.test/mcp?token=url-secret#frag "
        "Authorization: Bearer bearer-secret api_key=plain-secret "
        '{"api_key":"json-secret","token": "json-token"} '
        "X-Api-Key: header-secret token: colon-secret password: pass-secret"
    )

    assert "user:pass" not in redacted
    assert "url-secret" not in redacted
    assert "bearer-secret" not in redacted
    assert "plain-secret" not in redacted
    assert "json-secret" not in redacted
    assert "json-token" not in redacted
    assert "header-secret" not in redacted
    assert "colon-secret" not in redacted
    assert "pass-secret" not in redacted
    assert "[redacted]@example.test" in redacted
    assert '"api_key":"[redacted]"' in redacted
    assert "X-Api-Key: [redacted]" in redacted


def test_sdk_mcp_manager_reports_missing_bearer_token_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PULSARA_TEST_MISSING_SDK_MCP_TOKEN", raising=False)
    config = McpServerConfig(
        server_id="secure_sdk",
        transport=McpStreamableHttpConfig(
            url="https://example.test/mcp",
            bearer_token_env_var="PULSARA_TEST_MISSING_SDK_MCP_TOKEN",
        ),
        startup_timeout_ms=50,
    )

    async def run() -> None:
        manager = await SdkMcpClientManager.start((config,))
        try:
            snapshot = manager.snapshots[0]
            assert snapshot.status is McpServerStatus.NEEDS_AUTH
            assert "missing bearer token" in (snapshot.message or "")
            assert not snapshot.tools
        finally:
            await manager.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_sdk_mcp_manager_tool_call_timeout_is_bounded(tmp_path: Path) -> None:
    fixture = _write_sdk_slow_fixture(tmp_path)
    config = McpServerConfig(
        server_id="sdk_slow",
        transport=McpStdioConfig(command=sys.executable, args=(str(fixture),), cwd=tmp_path),
        startup_timeout_ms=3_000,
        tool_timeout_ms=3_000,
    )

    async def run() -> None:
        manager = await SdkMcpClientManager.start((config,))
        try:
            assert manager.snapshots[0].status is McpServerStatus.READY
            with pytest.raises(Exception):
                await manager.call_tool("sdk_slow", "slow", {"delay": 2.0}, timeout_ms=10)
        finally:
            await manager.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_sdk_tool_result_mapping_preserves_error_and_non_text_artifacts() -> None:
    image_bytes = b"fake-png"
    result = sdk_types.CallToolResult(
        content=[
            sdk_types.TextContent(text="model-visible error text"),
            sdk_types.ImageContent(
                data=base64.b64encode(image_bytes).decode("ascii"),
                mimeType="image/png",
            ),
        ],
        structuredContent={"status": "bad"},
        isError=True,
    )

    mapped = mcp_tool_result_from_sdk(result)

    assert mapped.is_error is True
    assert "model-visible error text" in mapped.output
    assert "[image:image/png] 8 bytes archived" in mapped.output
    assert "[structured_content]" in mapped.output
    assert mapped.structured_content == {"status": "bad"}
    assert [artifact.role for artifact in mapped.artifacts] == ["content_1_image", "structured_content"]
    assert mapped.artifacts[0].data == image_bytes
    assert mapped.artifacts[1].text and '"status": "bad"' in mapped.artifacts[1].text


def test_mcp_supervisor_reconciles_desired_state_and_closes(tmp_path: Path) -> None:
    fixture = _write_sdk_stdio_fixture(tmp_path)
    config = McpServerConfig(
        server_id="sdk_docs",
        transport=McpStdioConfig(command=sys.executable, args=(str(fixture),), cwd=tmp_path),
        startup_timeout_ms=3_000,
        tool_timeout_ms=3_000,
    )
    disabled = McpServerConfig(
        server_id="sdk_docs",
        transport=McpStdioConfig(command=sys.executable, args=(str(fixture),), cwd=tmp_path),
        enabled=False,
    )

    async def run() -> None:
        supervisor = McpServerSupervisor()
        manager = await supervisor.sync_servers((config,))
        assert manager is not None
        assert supervisor.snapshots[0].status is McpServerStatus.READY
        same_manager = await supervisor.sync_servers((config,))
        assert same_manager is not None
        assert [snapshot.config.server_id for snapshot in supervisor.snapshots] == ["sdk_docs"]

        closed_manager = await supervisor.sync_servers((disabled,))
        assert closed_manager is supervisor
        assert supervisor.snapshots[0].status is McpServerStatus.DISABLED
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_mcp_supervisor_retries_unready_server_after_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PULSARA_TEST_MISSING_SUPERVISOR_MCP_TOKEN", raising=False)
    config = McpServerConfig(
        server_id="secure",
        transport=McpStreamableHttpConfig(
            url="https://example.test/mcp",
            bearer_token_env_var="PULSARA_TEST_MISSING_SUPERVISOR_MCP_TOKEN",
        ),
        startup_timeout_ms=50,
    )

    async def run() -> None:
        supervisor = McpServerSupervisor(retry_base_seconds=60.0)
        first = await supervisor.sync_servers((config,))
        assert first is not None
        assert first.snapshots[0].status is McpServerStatus.NEEDS_AUTH

        second = await supervisor.sync_servers((config,))
        assert second is first

        supervisor._next_retry_monotonic["secure"] = 0.0
        third = await supervisor.sync_servers((config,))
        assert third is not None
        assert third is first
        assert third.snapshots[0].status is McpServerStatus.NEEDS_AUTH
        assert supervisor._retry_attempts["secure"] == 2
        await supervisor.aclose(timeout_seconds=1)

    asyncio.run(run())


def test_mcp_cli_add_list_doctor_and_reconnect_use_sdk_manager(tmp_path: Path) -> None:
    fixture = _write_sdk_stdio_fixture(tmp_path)
    config_path = tmp_path / "mcp.yaml"

    add_report = asyncio.run(
        _mcp_command(
            SimpleNamespace(
                mcp_command="add",
                config=str(config_path),
                scope="user",
                env_file=None,
                override_env=False,
                workspace=str(tmp_path),
                server_id="sdk-docs",
                mcp_stdio_command=sys.executable,
                arg=[str(fixture)],
                cwd=str(tmp_path),
                env=[],
                url=None,
                header=[],
                env_header=[],
                bearer_token_env_var=None,
                follow_redirects=False,
                disabled=False,
                required=False,
                startup_timeout_ms=3_000,
                tool_timeout_ms=3_000,
                parallel_tools=False,
                enabled_tool=[],
                disabled_tool=[],
            )
        )
    )
    assert add_report["mcp"] == "add"

    list_report = asyncio.run(
        _mcp_command(
            SimpleNamespace(
                mcp_command="list",
                config=str(config_path),
                env_file=None,
                override_env=False,
                workspace=str(tmp_path),
            )
        )
    )
    assert [server["server_id"] for server in list_report["servers"]] == ["sdk-docs"]

    doctor_report = asyncio.run(
        _mcp_command(
            SimpleNamespace(
                mcp_command="doctor",
                config=str(config_path),
                env_file=None,
                override_env=False,
                workspace=str(tmp_path),
            )
        )
    )
    assert doctor_report["ready_count"] == 1
    assert doctor_report["cache_policy"]["sdk_cache"] is False
    assert doctor_report["servers"][0]["tools"][0]["name"] == "lookup"

    reconnect_report = asyncio.run(
        _mcp_command(
            SimpleNamespace(
                mcp_command="reconnect",
                config=str(config_path),
                env_file=None,
                override_env=False,
                workspace=str(tmp_path),
            )
        )
    )
    assert reconnect_report["mcp"] == "reconnect"
    assert reconnect_report["ready_count"] == 1
    assert reconnect_report["servers"][0]["tools"][0]["name"] == "lookup"


def test_repl_mcp_startup_notice_summarizes_snapshots() -> None:
    manager = MockMcpClientManager(
        (
            _snapshot(_tool(), status=McpServerStatus.READY),
            McpServerSnapshot(
                config=McpServerConfig(
                    server_id="auth",
                    transport=McpStreamableHttpConfig(url="https://example.test/mcp"),
                ),
                status=McpServerStatus.NEEDS_AUTH,
                message="missing bearer token",
                diagnostics=({"code": "mcp_missing_auth"},),
            ),
        )
    )
    session = SimpleNamespace(
        wiring=SimpleNamespace(
            runtime_wiring=SimpleNamespace(mcp_manager=manager),
        )
    )

    notice = _format_repl_mcp_startup_notice(session)

    assert notice == "MCP servers: docs=ready (1 tools); auth=needs_auth (missing bearer token; 1 diagnostics)"


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


def test_mcp_adapter_does_not_accept_legacy_elicitation_argument(tmp_path: Path) -> None:
    manager = MockMcpClientManager(
        (_snapshot(_tool(read_only=True)),),
        handlers={("docs", "lookup"): lambda args: {"args": args}},
    )
    bundle = build_mcp_bundle(manager)
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        wiring = build_in_memory_runtime_wiring(tmp_path, mcp_bundle=bundle)
    executor = wiring.runtime_session.create_tool_executor()

    result = asyncio.run(
        executor.execute_async(
            ToolCall(
                id="call:mcp-legacy",
                name="mcp__docs__lookup",
                arguments={"__mcp_elicitation__": {"request_id": "fake"}},
            ),
            event_context=EventContext(run_id="run:1", turn_id="turn:1", reply_id="reply:1"),
            descriptor=bundle.descriptors[0],
        )
    )

    assert result.status is ToolResultState.SUCCESS
    assert manager.calls == [("docs", "lookup", {"__mcp_elicitation__": {"request_id": "fake"}})]


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


def test_mcp_supervisor_refreshes_ready_manager_snapshot_on_safe_point() -> None:
    @dataclass(slots=True)
    class RefreshingManager:
        _snapshots: tuple[McpServerSnapshot, ...]
        config: McpServerConfig
        refresh_count: int = 0

        @property
        def snapshots(self) -> tuple[McpServerSnapshot, ...]:
            return self._snapshots

        async def refresh(self) -> tuple[McpServerSnapshot, ...]:
            self.refresh_count += 1
            self._snapshots = (
                _snapshot_for_config(
                    self.config,
                    _tool("lookup"),
                    _tool("new_lookup"),
                    generation=8,
                ),
            )
            return self._snapshots

        async def call_tool(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("not used")

        async def resume_suspended_request(self, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("not used")

        async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
            return None

        def cancel_active(self) -> None:
            return None

    config = McpServerConfig(
        server_id="docs",
        transport=McpStreamableHttpConfig(url="http://127.0.0.1:8765/mcp"),
    )
    manager = RefreshingManager((_snapshot_for_config(config, _tool("lookup"), generation=7),), config=config)
    supervisor = McpServerSupervisor()
    supervisor._managers["docs"] = manager  # noqa: SLF001
    supervisor._fingerprints["docs"] = _config_fingerprint(config)  # noqa: SLF001

    synced = asyncio.run(supervisor.sync_servers((config,)))

    assert synced is supervisor
    assert manager.refresh_count == 1
    bundle = build_mcp_bundle(supervisor)
    assert [descriptor.metadata["original_tool_name"] for descriptor in bundle.descriptors] == [
        "lookup",
        "new_lookup",
    ]


def test_host_core_keeps_empty_mcp_supervisor_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pulsara_agent.host.core.load_mcp_server_configs",
        lambda *, workspace_root: (),
    )
    core = HostCore(settings=object(), durable=False)  # type: ignore[arg-type]
    workspace = resolve_workspace(HostWorkspaceInput(workspace_kind="transient", workspace_root=tmp_path))

    supervisor = asyncio.run(core._build_mcp_supervisor(workspace))

    assert isinstance(supervisor, McpServerSupervisor)
    assert supervisor.snapshots == ()


def test_host_session_mcp_refresh_preserves_non_mcp_bindings_and_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_tool = _LegacyMcpElicitationFixtureTool()
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    runtime_wiring.runtime_session.extra_tool_bindings = (fixture_tool,)
    provider = _FixtureCapabilityProvider(fixture_tool.name)
    agent = AgentRuntime(
        runtime_session=runtime_wiring.runtime_session,
        llm_runtime=_FinalTextRuntime(),
        capability_runtime=CapabilityRuntime.with_default_providers(provider),
        permission_policy=EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.ALLOW,
        ),
    )
    monkeypatch.setattr(
        host_session_module,
        "load_mcp_server_configs",
        lambda *, workspace_root: (
            McpServerConfig(
                server_id="disabled_docs",
                enabled=False,
                transport=McpStreamableHttpConfig(url="http://127.0.0.1:8765/mcp"),
            ),
        ),
    )
    session = HostSession(
        host_session_id="host:test",
        conversation_id="conversation:test",
        workspace=resolve_workspace(HostWorkspaceInput(workspace_kind="transient", workspace_root=tmp_path)),
        wiring=AgentRuntimeWiring(agent_runtime=agent, runtime_wiring=runtime_wiring),
        mcp_supervisor=McpServerSupervisor(),
    )

    asyncio.run(session._sync_mcp_servers_for_turn())

    assert fixture_tool in runtime_wiring.runtime_session.extra_tool_bindings
    assert not any(isinstance(tool, McpCapabilityTool) for tool in runtime_wiring.runtime_session.extra_tool_bindings)
    assert any(existing is provider for existing in agent.capability_runtime.providers)
    assert any(isinstance(existing, McpCapabilityProvider) for existing in agent.capability_runtime.providers)


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


def test_host_session_close_suppresses_mcp_manager_internal_cancel(tmp_path: Path) -> None:
    class CancelCloseMcpManager:
        snapshots = (_snapshot(_tool()),)
        close_count = 0

        async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
            self.close_count += 1
            raise asyncio.CancelledError("MCP SDK internal close cancellation")

        def cancel_active(self) -> None:
            pass

    manager = CancelCloseMcpManager()
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

    async def run() -> None:
        await session.aclose()
        await asyncio.sleep(0)
        assert manager.close_count == 1
        assert asyncio.current_task() is not None
        assert asyncio.current_task().cancelling() == 0

    asyncio.run(run())


def test_host_session_captures_and_resolves_mcp_elicitation(tmp_path: Path) -> None:
    fixture_tool = _LegacyMcpElicitationFixtureTool()
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    runtime_wiring.runtime_session.extra_tool_bindings = (fixture_tool,)
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:mcp",
                        "name": fixture_tool.name,
                        "arguments": json.dumps(
                            {
                                "request_id": "request-host",
                                "prompt": "Need a token",
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
        capability_runtime=CapabilityRuntime.with_default_providers(_FixtureCapabilityProvider(fixture_tool.name)),
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
    assert fixture_tool.responses == [
        ("docs", "request-host", {"value": "secret", "tool_call_id": "call:mcp"})
    ]


def test_host_session_captures_and_resolves_mcp_input_required(tmp_path: Path) -> None:
    session, manager, _, _ = _mcp_input_required_host_session(tmp_path)

    first = asyncio.run(session.run_turn("call mcp"))

    assert first.status.value == "waiting_user"
    pending = session.get_pending_interaction()
    assert isinstance(pending, PendingMcpInputRequired)
    assert pending.request_state is None
    assert pending.input_requests == (
        {"key": "token", "method": "elicitation/create", "params": {"message": "Need a token", "mode": "form"}},
    )
    assert pending.original_request == {
        "source_method": "tools/call",
        "tool_name": "lookup",
        "arguments": {"query": "pulsara"},
    }

    manager.handlers[("docs", "lookup")] = lambda args: {"resumed": args["query"]}
    final = asyncio.run(
        session.resolve_mcp_input_required(
            McpInputRequiredInteractionResolution(
                interaction_id=pending.interaction_id,
                responses={"token": {"value": "secret"}},
                cancelled=False,
            )
        )
    )

    assert final.status.value == "finished"
    assert session.get_pending_interaction() is None
    assert manager.calls[-1] == ("docs", "lookup", {"query": "pulsara"})


def test_mcp_input_required_resume_exposure_denial_emits_gate_decision(tmp_path: Path) -> None:
    session, _, runtime_wiring, agent = _mcp_input_required_host_session(tmp_path)
    first = asyncio.run(session.run_turn("call mcp"))
    assert first.status.value == "waiting_user"
    pending = session.get_pending_interaction()
    assert isinstance(pending, PendingMcpInputRequired)

    agent.refresh_capability_runtime(CapabilityRuntime(providers=()))
    if session._suspended_state is not None:
        session._suspended_state.scratchpad.pop("capability_exposure", None)

    final = asyncio.run(
        session.resolve_mcp_input_required(
            McpInputRequiredInteractionResolution(
                interaction_id=pending.interaction_id,
                responses={"token": {"value": "secret"}},
                cancelled=False,
            )
        )
    )

    assert final.status.value == "finished"
    gate_decisions = [
        event
        for event in runtime_wiring.event_log.iter()
        if isinstance(event, CapabilityGateDecisionEvent) and event.tool_call_id == "call:mcp-input"
    ]
    assert gate_decisions[-1].decision == "deny"
    assert gate_decisions[-1].reason_code == "capability_descriptor_missing"
    assert gate_decisions[-1].result_state is ToolResultState.ERROR


def test_mcp_input_required_resume_permission_denial_emits_gate_decision(tmp_path: Path) -> None:
    session, _, runtime_wiring, agent = _mcp_input_required_host_session(tmp_path)
    first = asyncio.run(session.run_turn("call mcp"))
    assert first.status.value == "waiting_user"
    pending = session.get_pending_interaction()
    assert isinstance(pending, PendingMcpInputRequired)

    agent.set_permission_policy(
        EffectivePermissionPolicy(
            profile=PermissionProfile.READ_ONLY,
            approval=ApprovalPolicy.NEVER,
            terminal=TerminalAccess.OFF,
        )
    )

    final = asyncio.run(
        session.resolve_mcp_input_required(
            McpInputRequiredInteractionResolution(
                interaction_id=pending.interaction_id,
                responses={"token": {"value": "secret"}},
                cancelled=False,
            )
        )
    )

    assert final.status.value == "finished"
    gate_decisions = [
        event
        for event in runtime_wiring.event_log.iter()
        if isinstance(event, CapabilityGateDecisionEvent) and event.tool_call_id == "call:mcp-input"
    ]
    assert gate_decisions[-1].decision == "deny"
    assert gate_decisions[-1].reason_code == "permission_denied"
    assert gate_decisions[-1].result_state is ToolResultState.DENIED
    assert gate_decisions[-1].permission_policy["profile"] == "read_only"


def test_mcp_input_required_resume_permission_wait_fails_closed_with_typed_deny(tmp_path: Path) -> None:
    session, manager, runtime_wiring, agent = _mcp_input_required_host_session(tmp_path)
    first = asyncio.run(session.run_turn("call mcp"))
    assert first.status.value == "waiting_user"
    pending = session.get_pending_interaction()
    assert isinstance(pending, PendingMcpInputRequired)

    agent.set_permission_policy(
        EffectivePermissionPolicy(
            profile=PermissionProfile.TRUSTED_HOST,
            approval=ApprovalPolicy.ON_REQUEST,
            terminal=TerminalAccess.ALLOW,
        )
    )

    final = asyncio.run(
        session.resolve_mcp_input_required(
            McpInputRequiredInteractionResolution(
                interaction_id=pending.interaction_id,
                responses={"token": {"value": "secret"}},
                cancelled=False,
            )
        )
    )

    assert final.status.value == "finished"
    assert manager.calls == [("docs", "lookup", {"query": "pulsara"})]
    gate_decisions = [
        event
        for event in runtime_wiring.event_log.iter()
        if isinstance(event, CapabilityGateDecisionEvent) and event.tool_call_id == "call:mcp-input"
    ]
    assert gate_decisions[-1].decision == "deny"
    assert gate_decisions[-1].reason_code == "mcp_resume_permission_approval_unsupported"
    assert gate_decisions[-1].reason_message == (
        "mcp_resume_permission_approval_unsupported: destructive tool requires user confirmation by approval policy"
    )
    assert gate_decisions[-1].result_state is ToolResultState.DENIED


def test_mcp_adapter_preserves_wrapper_tool_call_id_on_multi_round_input_required() -> None:
    class SecondRoundManager(MockMcpClientManager):
        async def resume_suspended_request(self, **kwargs):  # type: ignore[no-untyped-def]
            resolution = kwargs["resolution"]
            return McpInputRequired(
                interaction_id="mcp_input_required:second",
                server_id="docs",
                protocol_version="2026-07-28",
                request_state="state:2",
                input_requests=(
                    McpInputRequestDTO(
                        key="token2",
                        method="elicitation/create",
                        params={"message": "Need another token", "mode": "form"},
                    ),
                ),
                original_request=McpOriginalRequest(
                    source_method=McpRequestSourceMethod.TOOL_CALL,
                    tool_name="lookup",
                    arguments={"query": "pulsara"},
                ),
                round_count=resolution.round_count + 1,
            )

    manager = SecondRoundManager((_snapshot(_tool()),), handlers={})
    tool = McpCapabilityTool(
        name="mcp__docs__lookup",
        description="Lookup docs",
        parameters={"type": "object"},
        server_id="docs",
        original_tool_name="lookup",
        client_manager=manager,
        timeout_ms=1_000,
    )

    result = asyncio.run(
        tool.resume_input_required(
            original_request={
                "source_method": "tools/call",
                "tool_name": "lookup",
                "arguments": {"query": "pulsara"},
            },
            request_state="state:1",
            resolution=McpInputRequiredResolution(
                interaction_id="mcp_input_required:first",
                responses={"token": {"value": "secret"}},
                tool_call_id="call:mcp-input",
                input_requests=(
                    McpInputRequestDTO(
                        key="token",
                        method="elicitation/create",
                        params={"message": "Need token", "mode": "form"},
                    ),
                ),
                round_count=1,
            ),
            runtime_context=ToolRuntimeContext(
                runtime_session_id="runtime:test",
                event_context=EventContext(run_id="run:1", turn_id="turn:1", reply_id="reply:1"),
            ),
        )
    )

    assert isinstance(result, ToolExecutionSuspended)
    assert result.payload["wrapper_tool_call_id"] == "call:mcp-input"
    assert result.payload["tool_call_id"] == "call:mcp-input"
    assert result.payload["round_count"] == 2


def test_mcp_input_required_resume_failure_preserves_pending_interaction(tmp_path: Path) -> None:
    class FailingResumeManager(MockMcpClientManager):
        async def resume_suspended_request(self, **kwargs):  # type: ignore[no-untyped-def]
            raise ValueError(
                "invalid MCP input response from https://user:pass@example.test/mcp?token=url-secret "
                "Authorization: Bearer bearer-secret api_key=plain-secret"
            )

    def request_input(args: dict[str, object]) -> McpInputRequired:
        return McpInputRequired(
            interaction_id="mcp_input_required:retry",
            server_id="docs",
            protocol_version="2026-07-28",
            request_state=None,
            input_requests=(
                McpInputRequestDTO(
                    key="token",
                    method="elicitation/create",
                    params={"message": "Need a token", "mode": "form"},
                ),
            ),
            original_request=McpOriginalRequest(
                source_method=McpRequestSourceMethod.TOOL_CALL,
                tool_name="lookup",
                arguments=dict(args),
            ),
        )

    manager = FailingResumeManager((_snapshot(_tool()),), handlers={("docs", "lookup"): request_input})
    bundle = build_mcp_bundle(manager)
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        runtime_wiring = build_in_memory_runtime_wiring(tmp_path, mcp_bundle=bundle)
    transport = _ScriptedTransport(
        [
            {
                "tool_calls": [
                    {
                        "id": "call:mcp-input",
                        "name": "mcp__docs__lookup",
                        "arguments": json.dumps({"query": "pulsara"}),
                    }
                ]
            }
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
    pending = session.get_pending_interaction()
    assert first.status.value == "waiting_user"
    assert isinstance(pending, PendingMcpInputRequired)

    retry = asyncio.run(
        session.resolve_mcp_input_required(
            McpInputRequiredInteractionResolution(
                interaction_id=pending.interaction_id,
                responses={"token": {"value": "secret"}},
            )
        )
    )

    assert retry.status.value == "waiting_user"
    still_pending = session.get_pending_interaction()
    assert isinstance(still_pending, PendingMcpInputRequired)
    assert still_pending.interaction_id == pending.interaction_id
    failure_events = [
        event
        for event in runtime_wiring.event_log.iter()
        if isinstance(event, CustomEvent) and event.name == "mcp_input_required_resume_failed"
    ]
    assert failure_events
    message = failure_events[-1].value["message"]
    assert "user:pass" not in message
    assert "url-secret" not in message
    assert "bearer-secret" not in message
    assert "plain-secret" not in message


def test_mcp_elicitation_suspends_and_resume_routes_answer_to_manager(tmp_path: Path) -> None:
    fixture_tool = _LegacyMcpElicitationFixtureTool()
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        runtime_wiring = build_in_memory_runtime_wiring(tmp_path)
    runtime_wiring.runtime_session.extra_tool_bindings = (fixture_tool,)
    agent = AgentRuntime(
        runtime_session=runtime_wiring.runtime_session,
        llm_runtime=_FinalTextRuntime(),
        capability_runtime=CapabilityRuntime.with_default_providers(_FixtureCapabilityProvider(fixture_tool.name)),
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
                        name=fixture_tool.name,
                        arguments={
                            "request_id": "request-1",
                            "prompt": "Token please",
                            "schema": {"type": "object"},
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
    assert fixture_tool.responses == [
        ("docs", "request-1", {"value": "secret", "tool_call_id": "call:mcp"})
    ]
    assert any(message.role == "tool_result" and message.name == fixture_tool.name for message in state.messages)


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
