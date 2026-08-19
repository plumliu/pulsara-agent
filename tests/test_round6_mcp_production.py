from __future__ import annotations

import asyncio
import ast
from dataclasses import fields
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import subprocess
import sys
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import mcp_types as types
import pytest

from pulsara_agent.conversation_kernel.contracts import HostWriterGuard
from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.cli import build_parser, _mcp_command
from pulsara_agent.conversation_kernel.interaction import (
    KernelInteractionCoordinator,
)
from pulsara_agent.conversation_kernel.interaction_arbiter import (
    InteractionAdmissionHooks,
)
from pulsara_agent.conversation_kernel.context_sources import _render_mcp_catalog
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.live_control import SessionLiveControlOwner
from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenModelCallMemoryContext,
    FrozenModelVisibleMemoryProvenance,
    ModelVisibleMemoryProvenanceDisposition,
)
from pulsara_agent.conversation_kernel.mcp.input_required import (
    McpInputRequiredFailure,
    McpInputRequiredRoundOwner,
    McpInputRequiredUnsupported,
)
from pulsara_agent.conversation_kernel.mcp.contracts import (
    McpCatalogSnapshot,
    McpServerCatalogEntry,
)
from pulsara_agent.conversation_kernel.mcp.naming import mangle_mcp_tool_names
from pulsara_agent.conversation_kernel.mcp.sdk_facade import (
    BoundedMcpSdkClient,
    McpAdvertisedCapabilities,
    McpProtocolConformanceError,
    McpTransportOperationError,
    _SlotByteBudget,
    _enforce_http_network_policy,
)
from pulsara_agent.conversation_kernel.mcp.sdk_facade import _BoundedTransport
from pulsara_agent.conversation_kernel.mcp.supervisor import (
    McpHostSupervisor,
    McpPhysicalOutcomeUnknown,
    McpPhysicalConcurrencyKind,
    McpServerState,
    McpSnapshotStale,
    _resource_uri_matches_template,
)
from pulsara_agent.conversation_kernel.tool_surface import McpEffectKind
from pulsara_agent.conversation_kernel.mcp.wire import (
    DEFAULT_MCP_WIRE_BOUNDS,
    McpWireBoundExceeded,
    McpWireBounds,
    bounded_json_loads,
    validate_schema,
)
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)
from pulsara_agent.conversation_kernel.job_catalog import JOB_HANDLER_CATALOG
from pulsara_agent.conversation_kernel.repository import (
    AcceptedInteractionDecision,
    ConversationKernelRepository,
    build_prepared_tool_remote_identity_publication,
)
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    KernelToolAuthorizationKind,
    KernelToolInvocationContext,
)
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.mcp_config import (
    McpConfiguredEffect,
    McpHttpNetworkPolicy,
    StreamableHttpTransportConfig,
    load_mcp_server_configs,
)
from pulsara_agent.model_input.contracts import FrozenToolSpec, ModelInputScopeKind
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import (
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)
from pulsara_agent.primitives.context import thaw_json
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
    ToolCallStartPayload,
    live_digest,
)
from tests.support.postgres import verified_postgres_provider
from tests.support.round3 import ScriptedKernelModel, StaticContextSourceCollector


FIXTURE = Path(__file__).parent / "fixtures" / "round6_mcp_server.py"


def _enabled_memory_context() -> FrozenModelCallMemoryContext:
    return FrozenModelCallMemoryContext(
        FrozenModelVisibleMemoryProvenance(
            ModelVisibleMemoryProvenanceDisposition.COMPLETE,
            (),
        )
    )
HTTP_FIXTURE = Path(__file__).parent / "fixtures" / "round6_mcp_http_server.py"


def _mcp_tool_stream(tool_name: str) -> list[object]:
    arguments = '{"text":"postgres"}'
    return [
        ToolCallStartPayload("call:mcp", "call:mcp", tool_name),
        ToolCallDeltaPayload("call:mcp", "call:mcp", arguments),
        ToolCallEndPayload(
            block_identity="call:mcp",
            tool_call_id="call:mcp",
            tool_name=tool_name,
            arguments_json=arguments,
            utf8_bytes=len(arguments.encode("utf-8")),
            digest=live_digest(arguments),
        ),
    ]


def _mcp_empty_tool_stream(tool_name: str) -> list[object]:
    arguments = "{}"
    return [
        ToolCallStartPayload("call:mcp-long", "call:mcp-long", tool_name),
        ToolCallDeltaPayload("call:mcp-long", "call:mcp-long", arguments),
        ToolCallEndPayload(
            block_identity="call:mcp-long",
            tool_call_id="call:mcp-long",
            tool_name=tool_name,
            arguments_json=arguments,
            utf8_bytes=len(arguments.encode("utf-8")),
            digest=live_digest(arguments),
        ),
    ]


def _text_stream(text: str) -> list[object]:
    return [
        TextStartPayload("text:round6"),
        TextDeltaPayload("text:round6", text),
        TextEndPayload(
            "text:round6",
            text,
            len(text.encode("utf-8")),
            live_digest(text),
        ),
    ]


def _config(
    tmp_path: Path,
    *,
    endpoint: str | None = None,
    scope_policy: str = "ROOT_AND_SUBAGENTS",
    enabled: bool = True,
    default_tool_timeout_ms: int | None = None,
    default_effect: str = "AUTO",
):
    config = tmp_path / "mcp.yaml"
    if endpoint is None:
        transport = {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(FIXTURE)],
        }
    else:
        transport = {
            "type": "streamable_http",
            "endpoint": endpoint,
            "allow_http_localhost": True,
            "proved_stateless": True,
        }
    timeout_line = (
        f"    default_tool_timeout_ms: {default_tool_timeout_ms}\n"
        if default_tool_timeout_ms is not None
        else ""
    )
    effect_lines = (
        "    effect_policy:\n"
        f"      default_effect: {default_effect}\n"
        if default_effect != "AUTO"
        else ""
    )
    config.write_text(
        "servers:\n"
        "  fixture:\n"
        f"    enabled: {'true' if enabled else 'false'}\n"
        "    required: true\n"
        f"    scope_policy: {scope_policy}\n"
        "    supports_parallel_tool_calls: true\n"
        "    catalog_refresh_interval_ms: DISABLED\n"
        f"{timeout_line}"
        f"{effect_lines}"
        f"    transport: {json.dumps(transport)}\n",
        encoding="utf-8",
    )
    return load_mcp_server_configs(user_config_path=config)[0]


def test_round6_http_network_policy_is_typed_and_private_default_denied() -> None:
    denied = StreamableHttpTransportConfig(
        endpoint="https://127.0.0.1:9443/mcp",
    )
    with pytest.raises(ValueError, match="PRIVATE_NETWORK_DENIED"):
        asyncio.run(_enforce_http_network_policy(denied))

    explicit_local = StreamableHttpTransportConfig(
        endpoint="http://127.0.0.1:9443/mcp",
        allow_http_localhost=True,
    )
    asyncio.run(_enforce_http_network_policy(explicit_local))

    explicit_private = StreamableHttpTransportConfig(
        endpoint="https://127.0.0.1:9443/mcp",
        network_policy=McpHttpNetworkPolicy.ALLOW_PRIVATE,
    )
    asyncio.run(_enforce_http_network_policy(explicit_private))


def test_round6_public_http_resolution_is_pinned_with_logical_host_and_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        calls = 0

        async def getaddrinfo(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("93.184.216.34", 443),
                )
            ]

        monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
        pinned = await _enforce_http_network_policy(
            StreamableHttpTransportConfig(
                endpoint="https://mcp.example.test:9443/v1/mcp?mode=bounded"
            )
        )
        assert calls == 1
        assert pinned.url == "https://93.184.216.34:9443/v1/mcp?mode=bounded"
        assert pinned.host_header == "mcp.example.test:9443"
        assert pinned.sni_hostname == "mcp.example.test"

    asyncio.run(exercise())


def test_round6_slot_wire_budget_is_shared_and_released() -> None:
    budget = _SlotByteBudget(32)
    budget.reserve(16)
    budget.reserve(16)
    with pytest.raises(McpWireBoundExceeded, match="slot bound"):
        budget.reserve(1)
    budget.release(16)
    budget.reserve(1)
    budget.release(17)
    assert budget.used == 0


def test_round6_json_shape_is_rejected_before_object_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def forbidden_loads(_data):
        nonlocal called
        called = True
        raise AssertionError("json.loads ran before the structural bound")

    monkeypatch.setattr(
        "pulsara_agent.conversation_kernel.mcp.wire.json.loads",
        forbidden_loads,
    )
    body = b"[" + b",".join(b"[]" for _ in range(80)) + b"]"
    with pytest.raises(McpWireBoundExceeded, match="node bound"):
        bounded_json_loads(
            body,
            maximum_bytes=len(body),
            maximum_nodes=64,
            maximum_depth=8,
        )
    assert not called


@pytest.mark.parametrize(
    ("uri", "template", "matches"),
    (
        ("fixture://round6/users/alice", "fixture://round6/users/{name}", True),
        ("fixture://round6/tree/a/b", "fixture://round6/tree/{path*}", True),
        ("fixture://round6/search?q=mcp", "fixture://round6/search{?q}", True),
        ("fixture://round6/search", "fixture://round6/search{?q}", True),
        ("fixture://round6/search?admin=true", "fixture://round6/search{?q}", False),
        (
            "fixture://round6/search?q=mcp&admin=true",
            "fixture://round6/search{?q}",
            False,
        ),
        ("fixture://round6/item;id=7", "fixture://round6/item{;id}", True),
        (
            "fixture://round6/item;admin=true",
            "fixture://round6/item{;id}",
            False,
        ),
        ("fixture://round6/users/a/b", "fixture://round6/users/{name}", False),
        ("fixture://round6/value", "fixture://round6/{broken", False),
    ),
)
def test_round6_resource_template_instances_have_closed_admission(
    uri: str, template: str, matches: bool
) -> None:
    assert _resource_uri_matches_template(uri, template) is matches


def test_round6_resource_template_matcher_is_linear_and_adjacent_fail_closed() -> None:
    template = "fixture://round6/" + "{+x}" * 128
    started = monotonic()
    assert not _resource_uri_matches_template(
        "fixture://round6/" + "value" * 128,
        template,
    )
    assert monotonic() - started < 0.1


def test_round6_transport_exception_notification_is_typed(tmp_path: Path) -> None:
    async def exercise() -> None:
        seen: list[str] = []

        async def callback(value: str) -> None:
            seen.append(value)

        config = _config(tmp_path)
        client = BoundedMcpSdkClient(
            config,
            workspace_root=Path.cwd(),
            notification_callback=callback,
        )
        await client._handle_notification(  # noqa: SLF001
            McpProtocolConformanceError("bad carrier")
        )
        await client._handle_notification(RuntimeError("transport"))  # noqa: SLF001
        assert seen == [
            "pulsara/protocol_conformance_failure",
            "pulsara/transport_failure",
        ]

    asyncio.run(exercise())


async def _installed_runtime(tmp_path: Path):
    supervisor = McpHostSupervisor(
        session_id="session:round6",
        workspace_root=tmp_path,
        configs=(_config(tmp_path),),
    )
    await supervisor.start()
    runtime = supervisor.install_pending_at_safe_point()
    assert runtime is not None
    return supervisor, runtime


def test_round6_discovery_calls_only_negotiated_listing_capabilities(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        supervisor = McpHostSupervisor(
            session_id="session:tools-only",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
            client_factory=_ToolsOnlyFakeMcpClient,  # type: ignore[arg-type]
        )
        await supervisor.start()
        runtime = supervisor.install_pending_at_safe_point()
        assert runtime is not None
        try:
            candidate = runtime.candidates["fixture"]
            assert candidate.discovery_snapshot.tools
            assert candidate.discovery_snapshot.resources == ()
            assert candidate.discovery_snapshot.resource_templates == ()
            assert candidate.discovery_snapshot.prompts == ()
            assert supervisor.catalog_snapshot().servers[0].status is McpServerState.READY
        finally:
            runtime.release()
            await supervisor.aclose()

    asyncio.run(exercise())


def test_round6_host_discovery_reservation_covers_client_open(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "two-servers.yaml"
    transport = json.dumps(
        {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(FIXTURE)],
        }
    )
    config_path.write_text(
        "servers:\n"
        "  first:\n"
        "    enabled: true\n"
        "    required: true\n"
        "    catalog_refresh_interval_ms: DISABLED\n"
        f"    transport: {transport}\n"
        "  second:\n"
        "    enabled: true\n"
        "    required: true\n"
        "    catalog_refresh_interval_ms: DISABLED\n"
        f"    transport: {transport}\n",
        encoding="utf-8",
    )

    async def exercise() -> None:
        _DiscoveryReservationFakeMcpClient.active_open = 0
        _DiscoveryReservationFakeMcpClient.peak_open = 0
        supervisor = McpHostSupervisor(
            session_id="session:discovery-reservation",
            workspace_root=tmp_path,
            configs=load_mcp_server_configs(user_config_path=config_path),
            client_factory=_DiscoveryReservationFakeMcpClient,  # type: ignore[arg-type]
        )
        try:
            await supervisor.start()
            assert _DiscoveryReservationFakeMcpClient.peak_open == 1
        finally:
            await supervisor.aclose()

    asyncio.run(exercise())


def test_round6_long_remote_tool_name_has_bounded_settleable_identity(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        supervisor = McpHostSupervisor(
            session_id="session:long-remote-name",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
            client_factory=_LongNameFakeMcpClient,  # type: ignore[arg-type]
        )
        await supervisor.start()
        runtime = supervisor.install_pending_at_safe_point()
        assert runtime is not None
        try:
            semantic = next(
                item
                for item in runtime.root_tool_specs
                if item.remote_tool_name == _LongNameFakeMcpSession.remote_name
            )
            executor = runtime.executors[semantic.provider_tool_name]
            permit = executor.admit(
                session_id="session:long-remote-name",
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id="turn:long-remote-name",
                tool_call_id="call:long-remote-name",
            )
            permit.mark_attempt_accepted()
            result = await executor.invoke(permit, {})
            assert result.state == "SUCCESS"
            assert len(result.remote_identity.encode("utf-8")) <= 4_096
            assert _LongNameFakeMcpSession.remote_name not in result.remote_identity
            candidate = build_prepared_tool_remote_identity_publication(
                session_id="session:long-remote-name",
                attempt_id="attempt:long-remote-name",
                remote_identity=result.remote_identity,
                occurred_at=datetime.now(timezone.utc),
                actor_id="tool:mcp",
            )
            assert candidate.remote_identity == result.remote_identity
        finally:
            runtime.release()
            await supervisor.aclose()

    asyncio.run(exercise())


def test_round6_advertised_resource_template_instance_is_readable(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        supervisor = McpHostSupervisor(
            session_id="session:template-resource",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
            client_factory=_TemplateResourceFakeMcpClient,  # type: ignore[arg-type]
        )
        await supervisor.start()
        runtime = supervisor.install_pending_at_safe_point()
        assert runtime is not None
        arguments = {
            "server_id": "fixture",
            "uri": "fixture://round6/users/alice",
        }
        try:
            permit = runtime.admit_standard_operation(
                tool_name="read_mcp_resource",
                arguments=arguments,
                descriptor_fingerprint="sha256:resource-descriptor",
                session_id="session:template-resource",
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id="turn:template-resource",
                tool_call_id="call:template-resource",
            )
            assert permit is not None
            result = await runtime.invoke_standard(
                tool_name="read_mcp_resource",
                arguments=arguments,
                permit=permit,
                scope_kind=ModelInputScopeKind.ROOT,
            )
            assert result.state == "SUCCESS"
            assert b"fixture://round6/users/alice" in result.content
            with pytest.raises(ValueError, match="absent from the exact snapshot"):
                runtime.admit_standard_operation(
                    tool_name="read_mcp_resource",
                    arguments={
                        "server_id": "fixture",
                        "uri": "fixture://round6/users/alice/private",
                    },
                    descriptor_fingerprint="sha256:resource-descriptor",
                    session_id="session:template-resource",
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                    turn_id="turn:template-resource",
                    tool_call_id="call:template-resource-invalid",
                )
        finally:
            runtime.release()
            await supervisor.aclose()

    asyncio.run(exercise())


async def _parallel_probe(runtime, *, session_id: str) -> tuple[bytes, bytes]:
    semantic = next(
        item
        for item in runtime.root_tool_specs
        if item.remote_tool_name == "fixture_parallel_probe"
    )
    executor = runtime.executors[semantic.provider_tool_name]
    permits = tuple(
        executor.admit(
            session_id=session_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            turn_id="turn:parallel",
            tool_call_id=f"call:parallel:{index}",
        )
        for index in range(2)
    )
    for permit in permits:
        permit.mark_attempt_accepted()
    results = await asyncio.gather(
        *(executor.invoke(permit, {}) for permit in permits)
    )
    return results[0].content, results[1].content


class _FakeMcpSession:
    async def list_tools(self, *, params=None):
        del params
        return types.ListToolsResult(
            resultType="complete",
            tools=[
                types.Tool(
                    name="fake_echo",
                    description="One fake read-only tool.",
                    inputSchema={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                    annotations=types.ToolAnnotations(
                        readOnlyHint=True,
                        destructiveHint=False,
                        openWorldHint=False,
                    ),
                )
            ],
        )

    async def list_resources(self, *, params=None):
        del params
        return types.ListResourcesResult(resultType="complete", resources=[])

    async def list_resource_templates(self, *, params=None):
        del params
        return types.ListResourceTemplatesResult(
            resultType="complete", resourceTemplates=[]
        )

    async def list_prompts(self, *, params=None):
        del params
        return types.ListPromptsResult(resultType="complete", prompts=[])

    async def call_tool(self, *_args, **_kwargs):
        return types.CallToolResult(
            resultType="complete",
            content=[types.TextContent(type="text", text="fake-result")],
        )


class _FakeMcpClient:
    instances: list["_FakeMcpClient"] = []

    def __init__(
        self,
        config,
        *,
        workspace_root: Path,
        notification_callback,
    ) -> None:
        del workspace_root
        self.config = config
        self.notification_callback = notification_callback
        self.session = _FakeMcpSession()
        self.protocol_version = "2025-11-25"
        self.server_instructions = "fake instructions"
        self.advertised_capabilities = McpAdvertisedCapabilities(
            tools=True,
            resources=True,
            prompts=True,
        )
        self.supports_bounded_stateless_parallelism = False
        self.closed = False
        type(self).instances.append(self)

    async def open(self) -> None:
        return None

    def require_closed_result_type(self, result=None) -> str:
        return str(getattr(result, "result_type", "complete"))

    async def aclose(self) -> None:
        self.closed = True


class _ToolsOnlyFakeMcpSession(_FakeMcpSession):
    async def list_resources(self, *, params=None):
        del params
        raise AssertionError("resources listing is not advertised")

    async def list_resource_templates(self, *, params=None):
        del params
        raise AssertionError("resource templates are not advertised")

    async def list_prompts(self, *, params=None):
        del params
        raise AssertionError("prompts listing is not advertised")


class _ToolsOnlyFakeMcpClient(_FakeMcpClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.session = _ToolsOnlyFakeMcpSession()
        self.advertised_capabilities = McpAdvertisedCapabilities(
            tools=True,
            resources=False,
            prompts=False,
        )


class _LongNameFakeMcpSession(_FakeMcpSession):
    remote_name = "remote_" + "x" * 5_000

    async def list_tools(self, *, params=None):
        del params
        return types.ListToolsResult(
            resultType="complete",
            tools=[
                types.Tool(
                    name=self.remote_name,
                    description="A long-name read-only tool.",
                    inputSchema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    annotations=types.ToolAnnotations(readOnlyHint=True),
                )
            ],
        )


class _LongNameFakeMcpClient(_FakeMcpClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.session = _LongNameFakeMcpSession()


class _DiscoveryReservationFakeMcpClient(_FakeMcpClient):
    active_open = 0
    peak_open = 0

    async def open(self) -> None:
        type(self).active_open += 1
        type(self).peak_open = max(
            type(self).peak_open,
            type(self).active_open,
        )
        try:
            await asyncio.sleep(0.03)
        finally:
            type(self).active_open -= 1


class _UnsupportedInputFakeMcpSession(_FakeMcpSession):
    async def call_tool(self, *_args, **_kwargs):
        return types.InputRequiredResult(
            resultType="input_required",
            inputRequests={
                "human": types.ElicitRequest(
                    method="elicitation/create",
                    params={"message": "private", "requestedSchema": {}},
                )
            },
            requestState="opaque-private-state",
        )


class _UnsupportedInputFakeMcpClient(_FakeMcpClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.session = _UnsupportedInputFakeMcpSession()


class _TemplateResourceFakeMcpSession(_FakeMcpSession):
    async def list_resources(self, *, params=None):
        del params
        return types.ListResourcesResult(resultType="complete", resources=[])

    async def list_resource_templates(self, *, params=None):
        del params
        return types.ListResourceTemplatesResult(
            resultType="complete",
            resourceTemplates=[
                types.ResourceTemplate(
                    name="user",
                    uriTemplate="fixture://round6/users/{name}",
                )
            ],
        )

    async def read_resource(self, uri, **_kwargs):
        return types.ReadResourceResult(
            resultType="complete",
            contents=[
                types.TextResourceContents(
                    uri=str(uri),
                    mimeType="text/plain",
                    text=f"template:{uri}",
                )
            ],
        )


class _TemplateResourceFakeMcpClient(_FakeMcpClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.session = _TemplateResourceFakeMcpSession()


class _TransientFakeMcpClient(_FakeMcpClient):
    failures_remaining = 0

    async def open(self) -> None:
        if type(self).failures_remaining:
            type(self).failures_remaining -= 1
            raise ConnectionError("transient fixture connection failure")


class _SchemaChangingFakeMcpSession(_FakeMcpSession):
    description = "schema-v1"

    async def list_tools(self, *, params=None):
        result = await super().list_tools(params=params)
        result.tools[0].description = type(self).description
        return result


class _SchemaChangingFakeMcpClient(_FakeMcpClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.session = _SchemaChangingFakeMcpSession()


class _ProtocolFailureFakeMcpClient(_FakeMcpClient):
    def require_closed_result_type(self, result=None) -> str:
        if isinstance(result, types.CallToolResult):
            raise McpProtocolConformanceError(
                "MCP_RESULT_TYPE_CONFORMANCE_FAILED"
            )
        return super().require_closed_result_type(result)


class _TransportFailureFakeMcpSession(_FakeMcpSession):
    may_have_reached_server = False

    async def call_tool(self, *_args, **_kwargs):
        raise McpTransportOperationError(
            may_have_reached_server=type(self).may_have_reached_server
        )


class _TransportFailureFakeMcpClient(_FakeMcpClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.session = _TransportFailureFakeMcpSession()


class _OutputSchemaMismatchFakeMcpSession(_FakeMcpSession):
    async def list_tools(self, *, params=None):
        del params
        return types.ListToolsResult(
            resultType="complete",
            tools=[
                types.Tool(
                    name="fake_echo",
                    description="One fake structured read-only tool.",
                    inputSchema={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                    outputSchema={
                        "type": "object",
                        "properties": {"count": {"type": "integer"}},
                        "required": ["count"],
                        "additionalProperties": False,
                    },
                    annotations=types.ToolAnnotations(
                        readOnlyHint=True,
                        destructiveHint=False,
                        openWorldHint=False,
                    ),
                )
            ],
        )

    async def call_tool(self, *_args, **_kwargs):
        return types.CallToolResult(
            resultType="complete",
            content=[types.TextContent(type="text", text="private mismatch")],
            structuredContent={"count": "not-an-integer"},
        )


class _OutputSchemaMismatchFakeMcpClient(_FakeMcpClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.session = _OutputSchemaMismatchFakeMcpSession()


class _LargeSurfaceFakeMcpSession(_FakeMcpSession):
    async def list_tools(self, *, params=None):
        del params
        return types.ListToolsResult(
            resultType="complete",
            tools=[
                types.Tool(
                    name=f"tool_{index:02d}",
                    description="Bounded test tool.",
                    inputSchema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    annotations=types.ToolAnnotations(readOnlyHint=True),
                )
                for index in range(64)
            ],
        )


class _LargeSurfaceFakeMcpClient(_FakeMcpClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.session = _LargeSurfaceFakeMcpSession()


class _SlowStandardReadFakeMcpSession(_FakeMcpSession):
    async def list_resources(self, *, params=None):
        del params
        return types.ListResourcesResult(
            resultType="complete",
            resources=[
                types.Resource(
                    uri="fixture://slow/resource",
                    name="slow-resource",
                    mimeType="text/plain",
                )
            ],
        )

    async def read_resource(self, *_args, **_kwargs):
        await asyncio.sleep(10)
        raise AssertionError("bounded MCP standard read did not time out")


class _SlowStandardReadFakeMcpClient(_FakeMcpClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.session = _SlowStandardReadFakeMcpSession()


class _UnsupportedDialectFakeMcpSession(_FakeMcpSession):
    async def list_tools(self, *, params=None):
        result = await super().list_tools(params=params)
        result.tools[0].input_schema["$schema"] = "https://example.invalid/future"
        return result


class _UnsupportedDialectFakeMcpClient(_FakeMcpClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.session = _UnsupportedDialectFakeMcpSession()


def test_round6_stdio_discovery_direct_tool_resource_and_prompt(tmp_path: Path) -> None:
    async def exercise() -> None:
        supervisor, runtime = await _installed_runtime(tmp_path)
        try:
            assert len(runtime.root_tool_specs) == 4
            echo = next(
                item
                for item in runtime.root_tool_specs
                if item.remote_tool_name == "fixture_echo"
            )
            executor = runtime.executors[echo.provider_tool_name]
            permit = executor.admit(
                session_id="session:round6",
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id="turn:1",
                tool_call_id="tool-call:1",
            )
            permit.mark_attempt_accepted()
            result = await executor.invoke(permit, {"text": "hello"})
            assert result.state == "SUCCESS"
            assert b"fixture:hello" in result.content
            stdio_slot = runtime.slot_lease_by_server["fixture"]._slot  # noqa: SLF001
            assert (
                stdio_slot.concurrency_kind
                is McpPhysicalConcurrencyKind.SERIAL_SESSION
            )
            serial_results = await _parallel_probe(
                runtime, session_id="session:round6"
            )
            assert all(b"parallel:no" in item for item in serial_results)

            resource_permit = runtime.admit_standard_operation(
                tool_name="read_mcp_resource",
                arguments={
                    "server_id": "fixture",
                    "uri": "fixture://round6/resource",
                },
                descriptor_fingerprint="sha256:resource-descriptor",
                session_id="session:round6",
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id="turn:1",
                tool_call_id="tool-call:resource",
            )
            assert resource_permit is not None
            resource = await runtime.invoke_standard(
                tool_name="read_mcp_resource",
                arguments={
                    "server_id": "fixture",
                    "uri": "fixture://round6/resource",
                },
                permit=resource_permit,
                scope_kind=ModelInputScopeKind.ROOT,
            )
            assert b"round6 resource body" in resource.content

            prompt_permit = runtime.admit_standard_operation(
                tool_name="get_mcp_prompt",
                arguments={
                    "server_id": "fixture",
                    "prompt_name": "round6_prompt",
                    "arguments": {"topic": "MCP"},
                },
                descriptor_fingerprint="sha256:prompt-descriptor",
                session_id="session:round6",
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id="turn:1",
                tool_call_id="tool-call:prompt",
            )
            assert prompt_permit is not None
            prompt = await runtime.invoke_standard(
                tool_name="get_mcp_prompt",
                arguments={
                    "server_id": "fixture",
                    "prompt_name": "round6_prompt",
                    "arguments": {"topic": "MCP"},
                },
                permit=prompt_permit,
                scope_kind=ModelInputScopeKind.ROOT,
            )
            assert b"Discuss MCP" in prompt.content
        finally:
            runtime.release()
            await supervisor.aclose()

    asyncio.run(exercise())


def test_round6_runtime_only_reconnect_preserves_semantic_surface_until_safe_point(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        _FakeMcpClient.instances.clear()
        first_config = _config(tmp_path, default_tool_timeout_ms=10_000)
        supervisor = McpHostSupervisor(
            session_id="session:runtime-rebind",
            workspace_root=tmp_path,
            configs=(first_config,),
            client_factory=_FakeMcpClient,  # type: ignore[arg-type]
        )
        await supervisor.start()
        first = supervisor.install_pending_at_safe_point()
        assert first is not None
        second_config = _config(tmp_path, default_tool_timeout_ms=20_000)
        assert (
            first_config.semantic_config_fingerprint
            == second_config.semantic_config_fingerprint
        )
        assert (
            first_config.runtime_config_fingerprint
            != second_config.runtime_config_fingerprint
        )
        old_executor = next(iter(first.executors.values()))
        try:
            supervisor.reload_configs((second_config,))
            # Runtime replacement does not publish an empty semantic surface.
            assert supervisor.install_pending_at_safe_point() is None
            with pytest.raises(McpSnapshotStale):
                old_executor.admit(
                    session_id="session:runtime-rebind",
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                    turn_id="turn:old-runtime",
                    tool_call_id="call:old-runtime",
                )
            deadline = monotonic() + 2
            while monotonic() < deadline:
                task = supervisor._tasks.get("fixture")  # noqa: SLF001
                if task is not None and task.done():
                    task.result()
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("runtime-only MCP reconnect did not finish")
            second = supervisor.install_pending_at_safe_point()
            assert second is not None
            try:
                assert (
                    first.catalog_snapshot.semantic_fingerprint
                    == second.catalog_snapshot.semantic_fingerprint
                )
                assert (
                    first.root_tool_specs[0].descriptor_fingerprint
                    == second.root_tool_specs[0].descriptor_fingerprint
                )
                permit = next(iter(second.executors.values())).admit(
                    session_id="session:runtime-rebind",
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                    turn_id="turn:new-runtime",
                    tool_call_id="call:new-runtime",
                )
                permit.release()
            finally:
                second.release()
        finally:
            first.release()
            await supervisor.aclose()

    asyncio.run(exercise())


def test_round6_replacement_attempt_keeps_installed_catalog_status_stable(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        _FakeMcpClient.instances.clear()
        supervisor = McpHostSupervisor(
            session_id="session:refresh-status",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
            client_factory=_FakeMcpClient,  # type: ignore[arg-type]
        )
        await supervisor.start()
        first = supervisor.install_pending_at_safe_point()
        assert first is not None
        before = supervisor.catalog_snapshot()
        try:
            supervisor._start_connect("fixture")  # noqa: SLF001
            during = supervisor.catalog_snapshot()
            assert during.semantic_fingerprint == before.semantic_fingerprint
            assert during.servers[0].status is McpServerState.READY
            task = supervisor._tasks["fixture"]  # noqa: SLF001
            await task
            pending = supervisor.catalog_snapshot()
            assert pending.semantic_fingerprint == before.semantic_fingerprint
            assert pending.servers[0].status is McpServerState.READY
        finally:
            first.release()
            await supervisor.aclose()

    asyncio.run(exercise())


def test_round6_mcp_catalog_ref_only_is_a_strict_compact_degradation() -> None:
    catalog = McpCatalogSnapshot(
        owner_epoch=1,
        catalog_revision=1,
        servers=(
            McpServerCatalogEntry(
                server_id="fixture",
                display_name="Fixture",
                status=McpServerState.READY,
                required=False,
                exposed_tool_count=1,
                discovered_tool_count=1,
                resource_count=1,
                resource_template_count=0,
                prompt_count=1,
                bounded_tool_name_overview=("fixture_echo",),
                sanitized_instructions="bounded fixture instructions",
                stable_failure_category=None,
                tool_surface_semantic_fingerprint="sha256:" + "1" * 64,
                catalog_semantic_fingerprint="sha256:" + "2" * 64,
                scope_subagents=False,
            ),
        ),
        semantic_fingerprint="sha256:" + "3" * 64,
        presentation_fingerprint="sha256:" + "4" * 64,
    )

    full, compact, reference = _render_mcp_catalog(catalog)

    assert len(reference.encode("utf-8")) < len(compact.encode("utf-8"))
    assert len(compact.encode("utf-8")) < len(full.encode("utf-8"))
    assert "read_more" not in reference


def test_round6_terminal_failure_retires_only_exact_pending_slot(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        _FakeMcpClient.instances.clear()
        supervisor = McpHostSupervisor(
            session_id="session:exact-slot-failure",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
            client_factory=_FakeMcpClient,  # type: ignore[arg-type]
        )
        await supervisor.start()
        first = supervisor.install_pending_at_safe_point()
        assert first is not None
        installed = supervisor._installed["fixture"]  # noqa: SLF001
        try:
            supervisor._start_connect("fixture")  # noqa: SLF001
            await supervisor._tasks["fixture"]  # noqa: SLF001
            failed = supervisor._pending["fixture"]  # noqa: SLF001
            failed_slot = failed.slot_lease._slot  # noqa: SLF001
            assert failed_slot is not installed.slot_lease._slot  # noqa: SLF001
            failed_slot.report_transport_failure(
                "MCP_PROTOCOL_CONFORMANCE_FAILED", retryable=False
            )
            assert supervisor._installed["fixture"] is installed  # noqa: SLF001
            assert "fixture" not in supervisor._pending  # noqa: SLF001
            assert supervisor.catalog_snapshot().servers[0].status is McpServerState.READY
            deadline = monotonic() + 2
            while failed_slot in supervisor._all_slots and monotonic() < deadline:  # noqa: SLF001
                await asyncio.sleep(0.01)
            assert failed_slot not in supervisor._all_slots  # noqa: SLF001
            assert failed_slot.client.closed
            assert supervisor._slots["fixture"] is installed.slot_lease._slot  # noqa: SLF001
        finally:
            first.release()
            await supervisor.aclose()

    asyncio.run(exercise())


def test_round6_tool_failure_matrix_separates_exact_response_from_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def invoke_with(client_factory, *, call_id: str):
        supervisor = McpHostSupervisor(
            session_id="session:failure-matrix",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
            client_factory=client_factory,
        )
        await supervisor.start()
        runtime = supervisor.install_pending_at_safe_point()
        assert runtime is not None
        try:
            semantic = next(iter(runtime.root_tool_specs))
            executor = runtime.executors[semantic.provider_tool_name]
            permit = executor.admit(
                session_id="session:failure-matrix",
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id="turn:failure-matrix",
                tool_call_id=call_id,
            )
            permit.mark_attempt_accepted()
            return await executor.invoke(permit, {"text": "bounded"})
        finally:
            runtime.release()
            await supervisor.aclose()

    protocol_failure = asyncio.run(
        invoke_with(_ProtocolFailureFakeMcpClient, call_id="call:protocol")
    )
    assert protocol_failure.state == "SYSTEM_ERROR"
    assert b"MCP_RESULT_TYPE_CONFORMANCE_FAILED" in protocol_failure.content

    output_mismatch = asyncio.run(
        invoke_with(_OutputSchemaMismatchFakeMcpClient, call_id="call:output")
    )
    assert output_mismatch.state == "SYSTEM_ERROR"
    assert b"MCP_OUTPUT_SCHEMA_MISMATCH" in output_mismatch.content
    assert b"private mismatch" not in output_mismatch.content

    monkeypatch.setattr(
        "pulsara_agent.conversation_kernel.mcp.supervisor._render_typed_result",
        lambda _result: (_ for _ in ()).throw(ValueError("private payload")),
    )
    lowering_failure = asyncio.run(
        invoke_with(_FakeMcpClient, call_id="call:lowering")
    )
    assert lowering_failure.state == "SYSTEM_ERROR"
    assert b"MCP_RESULT_LOWERING_FAILED" in lowering_failure.content
    assert b"private payload" not in lowering_failure.content

    _TransportFailureFakeMcpSession.may_have_reached_server = False
    unwritten = asyncio.run(
        invoke_with(_TransportFailureFakeMcpClient, call_id="call:unwritten")
    )
    assert unwritten.state == "SYSTEM_ERROR"
    assert b"MCP_TRANSPORT_UNWRITTEN" in unwritten.content

    _TransportFailureFakeMcpSession.may_have_reached_server = True
    with pytest.raises(McpPhysicalOutcomeUnknown):
        asyncio.run(
            invoke_with(_TransportFailureFakeMcpClient, call_id="call:unknown")
        )


def test_round6_unsupported_input_required_preserves_external_effect_unknown(
    tmp_path: Path,
) -> None:
    async def invoke(default_effect: str):
        supervisor = McpHostSupervisor(
            session_id=f"session:input-required:{default_effect}",
            workspace_root=tmp_path,
            configs=(_config(tmp_path, default_effect=default_effect),),
            client_factory=_UnsupportedInputFakeMcpClient,  # type: ignore[arg-type]
        )
        await supervisor.start()
        runtime = supervisor.install_pending_at_safe_point()
        assert runtime is not None
        try:
            executor = next(iter(runtime.executors.values()))
            permit = executor.admit(
                session_id=f"session:input-required:{default_effect}",
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id="turn:input-required",
                tool_call_id=f"call:{default_effect}",
            )
            permit.mark_attempt_accepted()
            return await executor.invoke(permit, {"text": "bounded"})
        finally:
            runtime.release()
            await supervisor.aclose()

    read_only = asyncio.run(invoke("READ_ONLY"))
    assert read_only.state == "SYSTEM_ERROR"
    assert b"MCP_INPUT_REQUIRED_UNSUPPORTED" in read_only.content
    with pytest.raises(McpPhysicalOutcomeUnknown):
        asyncio.run(invoke("EXTERNAL_EFFECT"))


def test_round6_direct_surface_over_64_tools_fails_without_truncation(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        supervisor = McpHostSupervisor(
            session_id="session:surface-bound",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
            client_factory=_LargeSurfaceFakeMcpClient,  # type: ignore[arg-type]
        )
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:surface-bound",
            session_id="session:surface-bound",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port.bind_mcp_supervisor(supervisor)
        await supervisor.start()
        try:
            port.prepare_tool_surface_safe_point()
            with pytest.raises(
                RuntimeError, match="MCP_DIRECT_TOOL_SURFACE_BOUND_EXCEEDED"
            ):
                port.snapshot_tool_surface(
                    conversation_scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                )
        finally:
            await port.aclose()
            await supervisor.aclose()

    asyncio.run(exercise())


def test_round6_standard_remote_read_has_one_bounded_operation_timeout(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        supervisor = McpHostSupervisor(
            session_id="session:standard-timeout",
            workspace_root=tmp_path,
            configs=(_config(tmp_path, default_tool_timeout_ms=1_000),),
            client_factory=_SlowStandardReadFakeMcpClient,  # type: ignore[arg-type]
        )
        await supervisor.start()
        runtime = supervisor.install_pending_at_safe_point()
        assert runtime is not None
        try:
            permit = runtime.admit_standard_operation(
                tool_name="read_mcp_resource",
                arguments={
                    "server_id": "fixture",
                    "uri": "fixture://slow/resource",
                },
                descriptor_fingerprint="sha256:resource-descriptor",
                session_id="session:standard-timeout",
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id="turn:standard-timeout",
                tool_call_id="call:standard-timeout",
            )
            assert permit is not None
            started = monotonic()
            result = await runtime.invoke_standard(
                tool_name="read_mcp_resource",
                arguments={
                    "server_id": "fixture",
                    "uri": "fixture://slow/resource",
                },
                permit=permit,
                scope_kind=ModelInputScopeKind.ROOT,
            )
            assert monotonic() - started < 2
            assert result.state == "SYSTEM_ERROR"
            assert b"MCP_STANDARD_READ_TIMEOUT" in result.content
        finally:
            runtime.release()
            await supervisor.aclose()

    asyncio.run(exercise())


def test_round6_unsupported_schema_dialect_never_installs_candidate(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        supervisor = McpHostSupervisor(
            session_id="session:unsupported-dialect",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
            client_factory=_UnsupportedDialectFakeMcpClient,  # type: ignore[arg-type]
        )
        try:
            with pytest.raises(RuntimeError, match="required MCP server failed"):
                await supervisor.start()
            assert supervisor.install_pending_at_safe_point() is not None
            assert supervisor.catalog_snapshot().servers[0].exposed_tool_count == 0
        finally:
            await supervisor.aclose()

    asyncio.run(exercise())


def test_round6_dirty_fence_blocks_new_dispatch_but_pre_admission_drains(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        supervisor, runtime = await _installed_runtime(tmp_path)
        try:
            echo = next(
                item
                for item in runtime.root_tool_specs
                if item.remote_tool_name == "fixture_echo"
            )
            executor = runtime.executors[echo.provider_tool_name]
            admitted = executor.admit(
                session_id="session:round6",
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id="turn:1",
                tool_call_id="tool-call:before-dirty",
            )
            runtime.slot_lease_by_server["fixture"]._slot.mark_dirty()  # noqa: SLF001
            with pytest.raises(McpSnapshotStale):
                executor.admit(
                    session_id="session:round6",
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                    turn_id="turn:1",
                    tool_call_id="tool-call:after-dirty",
                )
            admitted.mark_attempt_accepted()
            known = await executor.invoke(admitted, {"text": "drain"})
            assert b"fixture:drain" in known.content
        finally:
            runtime.release()
            await supervisor.aclose()

    asyncio.run(exercise())


def test_round6_dirty_candidate_cannot_publish_at_safe_point(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        _FakeMcpClient.instances.clear()
        supervisor = McpHostSupervisor(
            session_id="session:dirty-candidate",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
            client_factory=_FakeMcpClient,  # type: ignore[arg-type]
        )
        await supervisor.start()
        candidate = supervisor._pending["fixture"]  # noqa: SLF001
        candidate.slot_lease._slot.mark_dirty()  # noqa: SLF001
        try:
            runtime = supervisor.install_pending_at_safe_point()
            assert runtime is not None
            try:
                assert runtime.root_tool_specs == ()
                assert "fixture" in supervisor._pending  # noqa: SLF001
            finally:
                runtime.release()
        finally:
            await supervisor.aclose()

    asyncio.run(exercise())


def test_round6_list_changed_storm_coalesces_and_installs_fresh_lease(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        _FakeMcpClient.instances.clear()
        supervisor = McpHostSupervisor(
            session_id="session:list-changed",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
            client_factory=_FakeMcpClient,  # type: ignore[arg-type]
        )
        await supervisor.start()
        first = supervisor.install_pending_at_safe_point()
        assert first is not None
        old_executor = next(iter(first.executors.values()))
        old_slot = first.slot_lease_by_server["fixture"]._slot  # noqa: SLF001
        try:
            await asyncio.gather(
                *(
                    _FakeMcpClient.instances[0].notification_callback(
                        "notifications/tools/list_changed"
                    )
                    for _ in range(32)
                )
            )
            assert old_slot.dirty_generation == 32
            with pytest.raises(McpSnapshotStale):
                old_executor.admit(
                    session_id="session:list-changed",
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                    turn_id="turn:stale",
                    tool_call_id="call:stale",
                )

            deadline = monotonic() + 3
            while monotonic() < deadline:
                task = supervisor._tasks.get("fixture")  # noqa: SLF001
                if task is not None and task.done() and len(_FakeMcpClient.instances) >= 2:
                    task.result()
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("coalesced MCP reconcile did not complete")
            assert len(_FakeMcpClient.instances) == 2
            second = supervisor.install_pending_at_safe_point()
            assert second is not None
            try:
                assert second.runtime_generation_id != first.runtime_generation_id
                fresh = next(iter(second.executors.values()))
                permit = fresh.admit(
                    session_id="session:list-changed",
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                    turn_id="turn:fresh",
                    tool_call_id="call:fresh",
                )
                permit.release()
            finally:
                second.release()
        finally:
            first.release()
            await supervisor.aclose()
        assert all(item.closed for item in _FakeMcpClient.instances)

    asyncio.run(exercise())


def test_round6_required_startup_retries_transient_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        _TransientFakeMcpClient.instances.clear()
        _TransientFakeMcpClient.failures_remaining = 1
        monkeypatch.setattr(
            "pulsara_agent.conversation_kernel.mcp.supervisor.random.uniform",
            lambda _low, _high: 0.001,
        )
        supervisor = McpHostSupervisor(
            session_id="session:required-retry",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
            client_factory=_TransientFakeMcpClient,  # type: ignore[arg-type]
            required_startup_timeout_seconds=1,
            connect_attempt_timeout_seconds=0.5,
        )
        await supervisor.start()
        runtime = supervisor.install_pending_at_safe_point()
        assert runtime is not None
        try:
            assert len(_TransientFakeMcpClient.instances) == 2
            assert supervisor.catalog_snapshot().servers[0].status.value == "READY"
        finally:
            runtime.release()
            await supervisor.aclose()

    asyncio.run(exercise())


def test_round6_direct_kernel_surface_executes_exact_mcp_generation(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        supervisor = McpHostSupervisor(
            session_id="session:surface",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
        )
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:surface",
            session_id="session:surface",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port.bind_mcp_supervisor(supervisor)
        await supervisor.start()
        port.prepare_tool_surface_safe_point()
        surface = port.snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        dynamic = next(
            item
            for item in surface.model_surface.tool_specs
            if item.name.endswith("fixture_echo")
        )
        borrow = port.borrow_tool_surface(surface)
        permission = build_run_permission_snapshot(
            snapshot_id="permission:surface",
            requested_mode=PermissionMode.READ_ONLY,
            effective_mode=PermissionMode.READ_ONLY,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        try:
            authorization = await port.authorize(
                tool_name=dynamic.name,
                arguments={"text": "surface"},
                tool_call_id="call:surface",
                turn_id="turn:surface",
                assistant_entry_id="entry:assistant",
                permission_snapshot=permission,
                surface_borrow=borrow,
                memory_context=_enabled_memory_context(),
            )
            assert authorization.kind is KernelToolAuthorizationKind.ALLOW
            invocation = KernelToolInvocationContext(
                session_id="session:surface",
                workspace_id="workspace:surface",
                turn_id="turn:surface",
                assistant_entry_id="entry:assistant",
                tool_call_id="call:surface",
                attempt_id="attempt:surface",
                result_entry_id="entry:result",
                conversation_scope_kind=ModelInputScopeKind.ROOT.value,
                scope_subagent_task_id=None,
                host_owner_epoch=1,
                authorization_reference=authorization.reference,
                permission_snapshot_fingerprint=permission.snapshot_fingerprint,
                attempt_permission_snapshot_fingerprint=(
                    permission.snapshot_fingerprint
                ),
                tool_surface_fingerprint=(
                    surface.model_surface.surface_fingerprint
                ),
                executor_binding_fingerprint=borrow.binding_fingerprint(
                    dynamic.name
                ),
                surface_borrow=borrow,
            )
            result = await port.invoke(
                tool_name=dynamic.name,
                arguments={"text": "surface"},
                tool_call_id="call:surface",
                attempt_id="attempt:surface",
                turn_id="turn:surface",
                assistant_entry_id="entry:assistant",
                invocation_context=invocation,
            )
            assert result.state == "SUCCESS"
            assert b"fixture:surface" in result.content
            assert result.effect_class == "read_only"
        finally:
            borrow.close()
            supervisor.stop_admission()
            supervisor_close = asyncio.create_task(supervisor.aclose())
            await port.aclose(timeout_seconds=5)
            await supervisor_close

    asyncio.run(exercise())


def test_round6_permission_matrix_is_local_and_scope_surface_is_stable(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        supervisor = McpHostSupervisor(
            session_id="session:policy",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
        )
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:policy",
            session_id="session:policy",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port.bind_mcp_supervisor(supervisor)
        await supervisor.start()
        port.prepare_tool_surface_safe_point()
        surface = port.snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        borrow = port.borrow_tool_surface(surface)
        echo = next(
            item
            for item in surface.model_surface.tool_specs
            if item.name.endswith("fixture_echo")
        )
        effect = next(
            item
            for item in surface.model_surface.tool_specs
            if item.name.endswith("fixture_effect")
        )
        try:
            decisions: dict[tuple[PermissionMode, str], KernelToolAuthorizationKind] = {}
            for mode in PermissionMode:
                permission = build_run_permission_snapshot(
                    snapshot_id=f"permission:{mode.value}",
                    requested_mode=mode,
                    effective_mode=mode,
                    admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
                )
                for semantic, arguments in (
                    (echo, {"text": "read"}),
                    (effect, {"value": "effect"}),
                ):
                    call_id = f"call:{mode.value}:{semantic.name}"
                    decision = await port.authorize(
                        tool_name=semantic.name,
                        arguments=arguments,
                        tool_call_id=call_id,
                        turn_id="turn:policy",
                        assistant_entry_id="entry:policy",
                        permission_snapshot=permission,
                        surface_borrow=borrow,
                        memory_context=_enabled_memory_context(),
                    )
                    decisions[(mode, semantic.name)] = decision.kind
            assert all(
                decisions[(mode, echo.name)] is KernelToolAuthorizationKind.ALLOW
                for mode in PermissionMode
            )
            assert (
                decisions[(PermissionMode.READ_ONLY, effect.name)]
                is KernelToolAuthorizationKind.PERMISSION_DENIED
            )
            assert (
                decisions[(PermissionMode.ASK_PERMISSIONS, effect.name)]
                is KernelToolAuthorizationKind.REQUIRE_CONFIRMATION
            )
            assert (
                decisions[(PermissionMode.ACCEPT_EDITS, effect.name)]
                is KernelToolAuthorizationKind.REQUIRE_CONFIRMATION
            )
            assert (
                decisions[(PermissionMode.BYPASS_PERMISSIONS, effect.name)]
                is KernelToolAuthorizationKind.ALLOW
            )
            assert surface.model_surface.surface_fingerprint
        finally:
            borrow.close()
            supervisor.stop_admission()
            close = asyncio.create_task(supervisor.aclose())
            await port.aclose(timeout_seconds=5)
            await close

        root_only = McpHostSupervisor(
            session_id="session:root-only",
            workspace_root=tmp_path,
            configs=(_config(tmp_path, scope_policy="ROOT_ONLY"),),
        )
        await root_only.start()
        runtime = root_only.install_pending_at_safe_point()
        assert runtime is not None
        try:
            assert runtime.root_tool_specs
            assert runtime.subagent_tool_specs == ()
            assert (
                runtime.catalog_snapshot.for_scope(
                    ModelInputScopeKind.SUBAGENT_TASK
                ).servers
                == ()
            )
            with pytest.raises(ValueError, match="not visible in this scope"):
                runtime.admit_standard_operation(
                    tool_name="read_mcp_resource",
                    arguments={
                        "server_id": "fixture",
                        "uri": "fixture://round6/resource",
                    },
                    descriptor_fingerprint="sha256:resource-descriptor",
                    session_id="session:root-only",
                    scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
                    scope_subagent_task_id="subagent-task:root-only",
                    turn_id="turn:child",
                    tool_call_id="call:child-resource",
                )
        finally:
            runtime.release()
            await root_only.aclose()

    asyncio.run(exercise())


@pytest.mark.postgres
def test_round6_postgres_runner_commits_attempt_before_real_mcp_effect(
    stage2_migrated_postgres_database,
    tmp_path: Path,
) -> None:
    provider = verified_postgres_provider(
        stage2_migrated_postgres_database.runtime_dsn
    )
    repository = ConversationKernelRepository(provider)
    session_id = f"session:round6:{uuid4().hex}"
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=f"workspace:{uuid4().hex}",
        writer_owner_id=f"host:{uuid4().hex}",
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )

    async def exercise() -> tuple[object, object, str, ScriptedKernelModel]:
        supervisor = McpHostSupervisor(
            session_id=session_id,
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
        )
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:round6-postgres",
            session_id=session_id,
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port.bind_mcp_supervisor(supervisor)
        await supervisor.start()
        port.prepare_tool_surface_safe_point()
        surface = port.snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        dynamic_name = next(
            item.name
            for item in surface.model_surface.tool_specs
            if item.name.endswith("fixture_echo")
        )
        model = ScriptedKernelModel(
            [
                _mcp_tool_stream(dynamic_name),
                _text_stream("mcp complete"),
                _text_stream("after reconnect"),
            ]
        )
        runner = ConversationKernelRunner(
            repository=repository,
            writer_lease=lease,
            model=model,
            tools=port,
            live_bus=LiveAgentEventBus(),
            context_source_collector=StaticContextSourceCollector(),
        )
        try:
            first_result = await runner.run_turn("use the configured MCP tool")
            supervisor.reconnect("fixture")
            connection = supervisor._tasks["fixture"]  # noqa: SLF001
            await connection
            second_result = await runner.run_turn("continue after reconnect")
            return first_result, second_result, dynamic_name, model
        finally:
            supervisor.stop_admission()
            close = asyncio.create_task(supervisor.aclose())
            await port.aclose(timeout_seconds=5)
            await close

    result, second_result, dynamic_name, model = asyncio.run(exercise())
    assert result.final_text == "mcp complete"
    assert result.tool_call_count == 1
    assert second_result.final_text == "after reconnect"
    before_reconnect = model.requests[1].compiled_input
    after_reconnect = model.requests[2].compiled_input
    assert after_reconnect.system_prompt == before_reconnect.system_prompt
    assert after_reconnect.tools == before_reconnect.tools
    assert (
        after_reconnect.messages[: len(before_reconnect.messages)]
        == before_reconnect.messages
    )
    rows = repository.rehydrate_session(
        session_id=session_id,
        deadline_monotonic=monotonic() + 30,
    )
    assert [row["entry_kind"] for row in rows] == [
        "USER_MESSAGE",
        "ASSISTANT_TOOL_REQUEST",
        "TOOL_RESULT",
        "ASSISTANT_MESSAGE",
        "USER_MESSAGE",
        "ASSISTANT_MESSAGE",
    ]
    assert dynamic_name in str(rows[1])
    assert "fixture:postgres" in str(rows[2])
    events = repository.events_after(
        session_id=session_id,
        after_sequence=0,
        limit=16,
        deadline_monotonic=monotonic() + 30,
    )
    event_types = tuple(row["event_type"] for row in events)
    required_order = (
        "UserMessageAccepted",
        "AssistantToolRequestAccepted",
        "CapabilityDecisionAccepted",
        "ToolAttemptAccepted",
        "ToolResultAccepted",
        "AssistantMessageAccepted",
    )
    assert tuple(sorted(required_order, key=event_types.index)) == required_order
    assert event_types.index("ToolAttemptAccepted") < event_types.index(
        "ToolRemoteIdentityPublished"
    ) < event_types.index("ToolResultAccepted")


@pytest.mark.postgres
def test_round6_long_remote_name_exact_result_reaches_canonical_acceptance(
    stage2_migrated_postgres_database,
    tmp_path: Path,
) -> None:
    provider = verified_postgres_provider(
        stage2_migrated_postgres_database.runtime_dsn
    )
    repository = ConversationKernelRepository(provider)
    session_id = f"session:round6-long:{uuid4().hex}"
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=f"workspace:{uuid4().hex}",
        writer_owner_id=f"host:{uuid4().hex}",
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )

    async def exercise() -> object:
        supervisor = McpHostSupervisor(
            session_id=session_id,
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
            client_factory=_LongNameFakeMcpClient,  # type: ignore[arg-type]
        )
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:round6-long",
            session_id=session_id,
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port.bind_mcp_supervisor(supervisor)
        await supervisor.start()
        port.prepare_tool_surface_safe_point()
        surface = port.snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        dynamic_name = next(
            item.name
            for item in surface.model_surface.tool_specs
            if item.name.startswith("mcp__")
        )
        runner = ConversationKernelRunner(
            repository=repository,
            writer_lease=lease,
            model=ScriptedKernelModel(
                [
                    _mcp_empty_tool_stream(dynamic_name),
                    _text_stream("long name settled"),
                ]
            ),
            tools=port,
            live_bus=LiveAgentEventBus(),
            context_source_collector=StaticContextSourceCollector(),
        )
        try:
            return await runner.run_turn("invoke the long-name MCP tool")
        finally:
            supervisor.stop_admission()
            close = asyncio.create_task(supervisor.aclose())
            await port.aclose(timeout_seconds=5)
            await close

    result = asyncio.run(exercise())
    assert result.final_text == "long name settled"
    assert result.tool_call_count == 1
    rows = repository.rehydrate_session(
        session_id=session_id,
        deadline_monotonic=monotonic() + 30,
    )
    assert [row["entry_kind"] for row in rows] == [
        "USER_MESSAGE",
        "ASSISTANT_TOOL_REQUEST",
        "TOOL_RESULT",
        "ASSISTANT_MESSAGE",
    ]
    events = repository.events_after(
        session_id=session_id,
        after_sequence=0,
        limit=16,
        deadline_monotonic=monotonic() + 30,
    )
    event_types = tuple(row["event_type"] for row in events)
    assert event_types.index("ToolRemoteIdentityPublished") < event_types.index(
        "ToolResultAccepted"
    )


def test_round6_same_schema_reconnect_keeps_semantics_and_old_borrow(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        supervisor, first = await _installed_runtime(tmp_path)
        second = None
        try:
            first_surface = tuple(
                (item.provider_tool_name, item.descriptor_fingerprint)
                for item in first.root_tool_specs
            )
            first_echo = next(
                item
                for item in first.root_tool_specs
                if item.remote_tool_name == "fixture_echo"
            )
            first_executor = first.executors[first_echo.provider_tool_name]
            admitted_old = first_executor.admit(
                session_id="session:round6",
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id="turn:1",
                tool_call_id="tool-call:old-admitted",
            )
            admitted_old.mark_attempt_accepted()
            supervisor.reconnect("fixture")
            deadline = monotonic() + 10
            while monotonic() < deadline:
                task = supervisor._tasks.get("fixture")  # noqa: SLF001
                if task is not None and task.done():
                    task.result()
                    second = supervisor.install_pending_at_safe_point()
                    if second is not None:
                        break
                await asyncio.sleep(0.01)
            assert second is not None
            assert tuple(
                (item.provider_tool_name, item.descriptor_fingerprint)
                for item in second.root_tool_specs
            ) == first_surface
            assert first.runtime_generation_id != second.runtime_generation_id
            # The exact pre-fence admission drains on its old client, while a
            # new admission cannot be minted from the retiring generation.
            old_known = await first_executor.invoke(
                admitted_old, {"text": "old"}
            )
            assert b"fixture:old" in old_known.content
            with pytest.raises(McpSnapshotStale):
                first_executor.admit(
                    session_id="session:round6",
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                    turn_id="turn:1",
                    tool_call_id="tool-call:old-after-fence",
                )
            second_echo = next(
                item
                for item in second.root_tool_specs
                if item.remote_tool_name == "fixture_echo"
            )
            second_executor = second.executors[second_echo.provider_tool_name]
            new_permit = second_executor.admit(
                session_id="session:round6",
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                turn_id="turn:1",
                tool_call_id="tool-call:new",
            )
            new_permit.mark_attempt_accepted()
            new_known = await second_executor.invoke(
                new_permit, {"text": "new"}
            )
            assert b"fixture:new" in new_known.content
        finally:
            first.release()
            if second is not None:
                second.release()
            await supervisor.aclose()

    asyncio.run(exercise())


def test_round6_schema_change_reconnect_requires_safe_point_rebase(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        _SchemaChangingFakeMcpClient.instances.clear()
        _SchemaChangingFakeMcpSession.description = "schema-v1"
        supervisor = McpHostSupervisor(
            session_id="session:schema-change",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
            client_factory=_SchemaChangingFakeMcpClient,  # type: ignore[arg-type]
        )
        await supervisor.start()
        first = supervisor.install_pending_at_safe_point()
        assert first is not None
        second = None
        try:
            first_fingerprint = first.root_tool_specs[0].descriptor_fingerprint
            _SchemaChangingFakeMcpSession.description = "schema-v2"
            supervisor.reconnect("fixture")
            deadline = monotonic() + 3
            while monotonic() < deadline:
                task = supervisor._tasks.get("fixture")  # noqa: SLF001
                if task is not None and task.done():
                    task.result()
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("schema-change reconnect did not finish")

            # Physical discovery alone does not mutate the installed runtime.
            assert first.root_tool_specs[0].descriptor_fingerprint == first_fingerprint
            second = supervisor.install_pending_at_safe_point()
            assert second is not None
            assert (
                second.root_tool_specs[0].descriptor_fingerprint
                != first_fingerprint
            )
            assert (
                second.catalog_snapshot.semantic_fingerprint
                != first.catalog_snapshot.semantic_fingerprint
            )
        finally:
            first.release()
            if second is not None:
                second.release()
            await supervisor.aclose()

    asyncio.run(exercise())


def test_round6_config_disable_rebuilds_surface_and_old_borrow_drains(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        supervisor = McpHostSupervisor(
            session_id="session:disable",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
        )
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:disable",
            session_id="session:disable",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port.bind_mcp_supervisor(supervisor)
        await supervisor.start()
        port.prepare_tool_surface_safe_point()
        old_surface = port.snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        old_dynamic = next(
            item
            for item in old_surface.model_surface.tool_specs
            if item.name.endswith("fixture_echo")
        )
        old_borrow = port.borrow_tool_surface(old_surface)
        disabled = _config(tmp_path, enabled=False)
        try:
            assert await port.reload_mcp_configs((disabled,)) == frozenset(
                {"fixture"}
            )
            port.prepare_tool_surface_safe_point()
            new_surface = port.snapshot_tool_surface(
                conversation_scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
            )
            assert not any(
                item.name.startswith("mcp__")
                for item in new_surface.model_surface.tool_specs
            )

            # A pinned surface borrow is not a physical dispatch permit.  Once
            # config disable fences the slot, only a permit admitted before the
            # fence may drain; authorization attempted afterward must fail.
            permission = build_run_permission_snapshot(
                snapshot_id="permission:disable",
                requested_mode=PermissionMode.READ_ONLY,
                effective_mode=PermissionMode.READ_ONLY,
                admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
            )
            authorization = await port.authorize(
                tool_name=old_dynamic.name,
                arguments={"text": "old-batch"},
                tool_call_id="call:disable-old",
                turn_id="turn:disable",
                assistant_entry_id="entry:disable",
                permission_snapshot=permission,
                surface_borrow=old_borrow,
                memory_context=_enabled_memory_context(),
            )
            assert (
                authorization.kind
                is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
            )
        finally:
            old_borrow.close()
            supervisor.stop_admission()
            close = asyncio.create_task(supervisor.aclose())
            await port.aclose(timeout_seconds=5)
            await close

    asyncio.run(exercise())


class _InteractionRepository:
    def accept_tool_interaction_decision(self, guard, **kwargs):
        del guard
        return AcceptedInteractionDecision(
            str(kwargs["decision_id"]),
            str(kwargs["command_id"]),
            str(kwargs["decision"]),
            str(kwargs["assistant_entry_id"]),
            str(kwargs["tool_call_id"]),
            kwargs["attempt_id"],
            kwargs["result_entry_id"],
            str(kwargs["permission_snapshot_fingerprint"]),
        )


class _BlockingInteractionRepository(_InteractionRepository):
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def accept_tool_interaction_decision(self, guard, **kwargs):
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError("test interaction settlement was not released")
        return super().accept_tool_interaction_decision(guard, **kwargs)


class _FailThenBlockingInteractionRepository(_InteractionRepository):
    def __init__(self) -> None:
        self.calls = 0
        self.retry_started = Event()
        self.release = Event()

    def accept_tool_interaction_decision(self, guard, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("first settlement failed")
        self.retry_started.set()
        if not self.release.wait(5):
            raise TimeoutError("retry settlement was not released")
        return super().accept_tool_interaction_decision(guard, **kwargs)


def test_round6_config_disable_cancels_visible_uncommitted_confirmation(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        supervisor = McpHostSupervisor(
            session_id="session:disable-confirmation",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
        )
        live = SessionLiveControlOwner(
            session_id="session:disable-confirmation", owner_epoch=1
        )
        coordinator = KernelInteractionCoordinator(
            repository=_InteractionRepository(),  # type: ignore[arg-type]
            guard=HostWriterGuard("session:disable-confirmation", 1, "host:1"),
            live_control=live,
            live_bus=LiveAgentEventBus(),
            io_owner=KernelSessionIO(),
        )
        assert coordinator.attach_controller("attachment:1")
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:1",
            session_id="session:disable-confirmation",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port.bind_interaction_port(coordinator)
        port.bind_mcp_supervisor(supervisor)
        await supervisor.start()
        port.prepare_tool_surface_safe_point()
        surface = port.snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        effect = next(
            item
            for item in surface.model_surface.tool_specs
            if item.name.endswith("fixture_effect")
        )
        borrow = port.borrow_tool_surface(surface)
        permission = build_run_permission_snapshot(
            snapshot_id="permission:disable-confirmation",
            requested_mode=PermissionMode.ASK_PERMISSIONS,
            effective_mode=PermissionMode.ASK_PERMISSIONS,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        try:
            authorization = await port.authorize(
                tool_name=effect.name,
                arguments={"value": "cancel-me"},
                tool_call_id="call:disable-confirmation",
                turn_id="turn:disable-confirmation",
                assistant_entry_id="entry:disable-confirmation",
                permission_snapshot=permission,
                surface_borrow=borrow,
                memory_context=_enabled_memory_context(),
            )
            assert (
                authorization.kind
                is KernelToolAuthorizationKind.REQUIRE_CONFIRMATION
            )
            waiter = asyncio.create_task(
                port.request_confirmation(
                    tool_name=effect.name,
                    tool_call_id="call:disable-confirmation",
                    turn_id="turn:disable-confirmation",
                    assistant_entry_id="entry:disable-confirmation",
                    permission_snapshot=permission,
                )
            )
            await asyncio.sleep(0)
            assert live.current_snapshot().current_interaction is not None
            assert await port.reload_mcp_configs(()) == frozenset({"fixture"})
            denied = await waiter
            assert denied.kind is KernelToolAuthorizationKind.PERMISSION_DENIED
            assert live.current_snapshot().current_interaction is None
            assert not port._mcp_dispatch_permits  # noqa: SLF001
            assert not port._mcp_confirmation_admissions  # noqa: SLF001
        finally:
            borrow.close()
            await coordinator.aclose()
            supervisor.stop_admission()
            close = asyncio.create_task(supervisor.aclose())
            await port.aclose(timeout_seconds=5)
            await close

    asyncio.run(exercise())


def test_round6_interaction_close_joins_started_canonical_resolution() -> None:
    async def exercise() -> None:
        repository = _BlockingInteractionRepository()
        owner = SessionLiveControlOwner(session_id="session:close", owner_epoch=1)
        coordinator = KernelInteractionCoordinator(
            repository=repository,  # type: ignore[arg-type]
            guard=HostWriterGuard("session:close", 1, "host:1"),
            live_control=owner,
            live_bus=LiveAgentEventBus(),
            io_owner=KernelSessionIO(),
        )
        assert coordinator.attach_controller("attachment:1")
        permission = build_run_permission_snapshot(
            snapshot_id="permission:close",
            requested_mode=PermissionMode.ASK_PERMISSIONS,
            effective_mode=PermissionMode.ASK_PERMISSIONS,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        waiter = asyncio.create_task(
            coordinator.request_tool_confirmation(
                turn_id="turn:close",
                assistant_entry_id="entry:close",
                tool_call_id="call:close",
                tool_name="tool_close",
                permission_snapshot=permission,
            )
        )
        await asyncio.sleep(0)
        snapshot = owner.current_snapshot()
        assert snapshot.current_interaction is not None
        resolution = asyncio.create_task(
            coordinator.resolve_tool_interaction(
                expected_writer_generation=1,
                expected_owner_epoch=1,
                expected_live_revision=snapshot.revision,
                interaction_id=snapshot.current_interaction.interaction_id,
                command_id="command:close",
                decision="ALLOW",
                actor_id="attachment:1",
            )
        )
        assert await asyncio.to_thread(repository.started.wait, 2)
        close = asyncio.create_task(coordinator.aclose())
        await asyncio.sleep(0)
        assert not close.done()
        repository.release.set()
        await resolution
        assert (await waiter).decision == "ALLOW"
        await close

    asyncio.run(exercise())


def test_round6_interaction_retry_installs_fresh_unsettled_edge() -> None:
    async def exercise() -> None:
        repository = _FailThenBlockingInteractionRepository()
        owner = SessionLiveControlOwner(session_id="session:retry", owner_epoch=1)
        coordinator = KernelInteractionCoordinator(
            repository=repository,  # type: ignore[arg-type]
            guard=HostWriterGuard("session:retry", 1, "host:1"),
            live_control=owner,
            live_bus=LiveAgentEventBus(),
            io_owner=KernelSessionIO(),
        )
        assert coordinator.attach_controller("attachment:retry")
        permission = build_run_permission_snapshot(
            snapshot_id="permission:retry",
            requested_mode=PermissionMode.ASK_PERMISSIONS,
            effective_mode=PermissionMode.ASK_PERMISSIONS,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        waiter = asyncio.create_task(
            coordinator.request_tool_confirmation(
                turn_id="turn:retry",
                assistant_entry_id="entry:retry",
                tool_call_id="call:retry",
                tool_name="tool_retry",
                permission_snapshot=permission,
            )
        )
        await asyncio.sleep(0)
        snapshot = owner.current_snapshot()
        assert snapshot.current_interaction is not None
        kwargs = {
            "expected_writer_generation": 1,
            "expected_owner_epoch": 1,
            "expected_live_revision": snapshot.revision,
            "interaction_id": snapshot.current_interaction.interaction_id,
            "command_id": "command:retry",
            "decision": "ALLOW",
            "actor_id": "attachment:retry",
        }
        with pytest.raises(RuntimeError, match="first settlement failed"):
            await coordinator.resolve_tool_interaction(**kwargs)
        retry = asyncio.create_task(coordinator.resolve_tool_interaction(**kwargs))
        assert await asyncio.to_thread(repository.retry_started.wait, 2)
        pending = coordinator._pending  # noqa: SLF001
        assert pending is not None and pending.settlement_changed is not None
        assert not pending.settlement_changed.is_set()
        detached = asyncio.create_task(
            coordinator.controller_detached("attachment:retry")
        )
        await asyncio.sleep(0.02)
        assert not detached.done()
        repository.release.set()
        await asyncio.wait_for(retry, timeout=2)
        await asyncio.wait_for(detached, timeout=2)
        assert (await waiter).decision == "ALLOW"
        await coordinator.aclose()

    asyncio.run(exercise())


def test_round6_mcp_confirmation_admits_before_publish_and_drains_dirty(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        supervisor = McpHostSupervisor(
            session_id="session:confirmation",
            workspace_root=tmp_path,
            configs=(_config(tmp_path),),
        )
        live = SessionLiveControlOwner(
            session_id="session:confirmation", owner_epoch=1
        )
        coordinator = KernelInteractionCoordinator(
            repository=_InteractionRepository(),  # type: ignore[arg-type]
            guard=HostWriterGuard("session:confirmation", 1, "host:1"),
            live_control=live,
            live_bus=LiveAgentEventBus(),
            io_owner=KernelSessionIO(),
        )
        assert coordinator.attach_controller("attachment:1")
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:1",
            session_id="session:confirmation",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port.bind_interaction_port(coordinator)
        port.bind_mcp_supervisor(supervisor)
        await supervisor.start()
        port.prepare_tool_surface_safe_point()
        surface = port.snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        effect = next(
            item
            for item in surface.model_surface.tool_specs
            if item.name.endswith("fixture_effect")
        )
        borrow = port.borrow_tool_surface(surface)
        permission = build_run_permission_snapshot(
            snapshot_id="permission:confirmation",
            requested_mode=PermissionMode.ASK_PERMISSIONS,
            effective_mode=PermissionMode.ASK_PERMISSIONS,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        try:
            authorization = await port.authorize(
                tool_name=effect.name,
                arguments={"value": "confirmed"},
                tool_call_id="call:confirmation",
                turn_id="turn:confirmation",
                assistant_entry_id="entry:assistant",
                permission_snapshot=permission,
                surface_borrow=borrow,
                memory_context=_enabled_memory_context(),
            )
            assert (
                authorization.kind
                is KernelToolAuthorizationKind.REQUIRE_CONFIRMATION
            )
            assert not port._mcp_dispatch_permits  # noqa: SLF001
            waiter = asyncio.create_task(
                port.request_confirmation(
                    tool_name=effect.name,
                    tool_call_id="call:confirmation",
                    turn_id="turn:confirmation",
                    assistant_entry_id="entry:assistant",
                    permission_snapshot=permission,
                )
            )
            await asyncio.sleep(0)
            assert len(port._mcp_dispatch_permits) == 1  # noqa: SLF001
            runtime = port._mcp_current  # noqa: SLF001
            assert runtime is not None
            runtime.slot_lease_by_server["fixture"]._slot.mark_dirty()  # noqa: SLF001
            snapshot = live.current_snapshot()
            assert snapshot.current_interaction is not None
            await coordinator.resolve_tool_interaction(
                expected_writer_generation=1,
                expected_owner_epoch=1,
                expected_live_revision=snapshot.revision,
                interaction_id=snapshot.current_interaction.interaction_id,
                command_id="command:confirmation",
                decision="ALLOW",
                actor_id="attachment:1",
            )
            allowed = await waiter
            assert allowed.kind is KernelToolAuthorizationKind.ALLOW
            assert allowed.accepted_attempt_id is not None
            invocation = KernelToolInvocationContext(
                session_id="session:confirmation",
                workspace_id="workspace:confirmation",
                turn_id="turn:confirmation",
                assistant_entry_id="entry:assistant",
                tool_call_id="call:confirmation",
                attempt_id=allowed.accepted_attempt_id,
                result_entry_id="entry:result",
                conversation_scope_kind=ModelInputScopeKind.ROOT.value,
                scope_subagent_task_id=None,
                host_owner_epoch=1,
                authorization_reference=allowed.reference,
                permission_snapshot_fingerprint=permission.snapshot_fingerprint,
                attempt_permission_snapshot_fingerprint=(
                    permission.snapshot_fingerprint
                ),
                tool_surface_fingerprint=(
                    surface.model_surface.surface_fingerprint
                ),
                executor_binding_fingerprint=borrow.binding_fingerprint(
                    effect.name
                ),
                surface_borrow=borrow,
            )
            result = await port.invoke(
                tool_name=effect.name,
                arguments={"value": "confirmed"},
                tool_call_id="call:confirmation",
                attempt_id=allowed.accepted_attempt_id,
                turn_id="turn:confirmation",
                assistant_entry_id="entry:assistant",
                invocation_context=invocation,
            )
            assert b"effect:confirmed" in result.content
            assert result.effect_class == "unknown_effect"
        finally:
            borrow.close()
            await coordinator.aclose()
            supervisor.stop_admission()
            supervisor_close = asyncio.create_task(supervisor.aclose())
            await port.aclose(timeout_seconds=5)
            await supervisor_close

    asyncio.run(exercise())


def test_round6_confirmation_arbiter_is_single_visible_fifo() -> None:
    async def exercise() -> None:
        owner = SessionLiveControlOwner(session_id="session:1", owner_epoch=1)
        coordinator = KernelInteractionCoordinator(
            repository=_InteractionRepository(),  # type: ignore[arg-type]
            guard=HostWriterGuard("session:1", 1, "host:1"),
            live_control=owner,
            live_bus=LiveAgentEventBus(),
            io_owner=KernelSessionIO(),
        )
        assert coordinator.attach_controller("attachment:1")
        admission_order: list[str] = []
        permission = build_run_permission_snapshot(
            snapshot_id="permission:1",
            requested_mode=PermissionMode.ASK_PERMISSIONS,
            effective_mode=PermissionMode.ASK_PERMISSIONS,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )

        def request(index: int):
            return coordinator.request_tool_confirmation(
                turn_id="turn:1",
                assistant_entry_id="entry:1",
                tool_call_id=f"call:{index}",
                tool_name=f"tool_{index}",
                permission_snapshot=permission,
                admission_hooks=InteractionAdmissionHooks(
                    before_publish=lambda: admission_order.append(str(index)),
                    discard=lambda: None,
                ),
            )

        first = asyncio.create_task(request(1))
        second = asyncio.create_task(request(2))
        await asyncio.sleep(0)
        assert admission_order == ["1"]
        snapshot = owner.current_snapshot()
        assert snapshot.current_interaction is not None
        await coordinator.resolve_tool_interaction(
            expected_writer_generation=1,
            expected_owner_epoch=1,
            expected_live_revision=snapshot.revision,
            interaction_id=snapshot.current_interaction.interaction_id,
            command_id="command:1",
            decision="ALLOW",
            actor_id="attachment:1",
        )
        assert (await first).decision == "ALLOW"
        assert admission_order == ["1", "2"]
        snapshot = owner.current_snapshot()
        assert snapshot.current_interaction is not None
        await coordinator.resolve_tool_interaction(
            expected_writer_generation=1,
            expected_owner_epoch=1,
            expected_live_revision=snapshot.revision,
            interaction_id=snapshot.current_interaction.interaction_id,
            command_id="command:2",
            decision="DENY",
            actor_id="attachment:1",
        )
        assert (await second).decision == "DENY"
        await coordinator.aclose()

    asyncio.run(exercise())


def test_round6_input_required_is_state_only_and_bounded() -> None:
    import mcp_types as types

    owner = McpInputRequiredRoundOwner("operation:1", 1)
    state = owner.prepare_state_only_continuation(
        types.InputRequiredResult(
            resultType="input_required",
            inputRequests={},
            requestState="opaque-state",
        )
    )
    assert state == "opaque-state"
    with pytest.raises(McpInputRequiredUnsupported) as caught:
        owner.prepare_state_only_continuation(
            types.InputRequiredResult(
                resultType="input_required",
                inputRequests={
                    "human": types.ElicitRequest(
                        method="elicitation/create",
                        params={"message": "secret", "requestedSchema": {}},
                    )
                },
                requestState="opaque-state-2",
            )
        )
    assert caught.value.failure is McpInputRequiredFailure.ELICITATION_UNSUPPORTED

    bounded = McpInputRequiredRoundOwner("operation:bounded", 1)
    for ordinal in range(16):
        assert bounded.prepare_state_only_continuation(
            types.InputRequiredResult(
                resultType="input_required",
                inputRequests={},
                requestState=f"state:{ordinal}",
            )
        ) == f"state:{ordinal}"
    with pytest.raises(McpInputRequiredUnsupported) as capped:
        bounded.prepare_state_only_continuation(
            types.InputRequiredResult(
                resultType="input_required",
                inputRequests={},
                requestState="state:overflow",
            )
        )
    assert capped.value.failure is McpInputRequiredFailure.ROUND_LIMIT_EXCEEDED
    assert "state:overflow" not in repr(bounded)

    with pytest.raises(McpInputRequiredUnsupported) as oversized_key:
        McpInputRequiredRoundOwner("operation:key", 1).prepare_state_only_continuation(
            types.InputRequiredResult(
                resultType="input_required",
                inputRequests={
                    "k" * 257: types.ListRootsRequest(
                        method="roots/list",
                        params={},
                    )
                },
                requestState="opaque",
            )
        )
    assert (
        oversized_key.value.failure
        is McpInputRequiredFailure.PHYSICAL_BOUND_EXCEEDED
    )


def test_round6_naming_disambiguates_normalization_collisions() -> None:
    canonical = mangle_mcp_tool_names("foo_bar", ("get_issue",))
    normalized = mangle_mcp_tool_names("foo-bar", ("get_issue",))
    assert canonical["get_issue"] != normalized["get_issue"]
    within_server = mangle_mcp_tool_names("server", ("x-y", "x_y"))
    assert len(set(within_server.values())) == 2


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_round6_streamable_http_fixture(tmp_path: Path) -> None:
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, str(HTTP_FIXTURE)],
        env={**dict(__import__("os").environ), "PULSARA_ROUND6_MCP_PORT": str(port)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = monotonic() + 10
        while monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            sleep(0.02)
        else:
            pytest.fail("MCP HTTP fixture did not start")

        async def exercise() -> None:
            config = _config(tmp_path, endpoint=f"http://127.0.0.1:{port}/mcp")
            client = BoundedMcpSdkClient(
                config,
                workspace_root=tmp_path,
                notification_callback=lambda _method: asyncio.sleep(0),
            )
            await client.open()
            try:
                tools = await client.session.list_tools()
                assert client.require_closed_result_type(tools) == "complete"
                assert {item.name for item in tools.tools} == {
                    "fixture_delay",
                    "fixture_echo",
                    "fixture_effect",
                    "fixture_parallel_probe",
                }
            finally:
                await client.aclose()

            supervisor = McpHostSupervisor(
                session_id="session:http-parallel",
                workspace_root=tmp_path,
                configs=(config,),
            )
            await supervisor.start()
            runtime = supervisor.install_pending_at_safe_point()
            assert runtime is not None
            try:
                slot = runtime.slot_lease_by_server["fixture"]._slot  # noqa: SLF001
                assert (
                    slot.concurrency_kind
                    is McpPhysicalConcurrencyKind.BOUNDED_STATELESS_HTTP
                )
                parallel_results = await _parallel_probe(
                    runtime, session_id="session:http-parallel"
                )
                assert all(b"parallel:yes" in item for item in parallel_results)
            finally:
                runtime.release()
                await supervisor.aclose()

        asyncio.run(exercise())
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_round6_stdio_eof_fences_exact_slot_and_schedules_reconnect(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        supervisor, runtime = await _installed_runtime(tmp_path)
        slot = runtime.slot_lease_by_server["fixture"]._slot  # noqa: SLF001
        transport = slot.client._transport  # noqa: SLF001
        process = transport._process  # noqa: SLF001
        assert process is not None
        try:
            process.terminate()
            await process.wait()
            deadline = monotonic() + 1
            while monotonic() < deadline:
                if "fixture" not in supervisor._installed:  # noqa: SLF001
                    break
                await asyncio.sleep(0.01)
            assert "fixture" not in supervisor._installed  # noqa: SLF001
            assert supervisor.catalog_snapshot().servers[0].status in {
                McpServerState.FAILED_RETRYABLE,
                McpServerState.CONNECTING,
            }
            assert "fixture" in supervisor._retry_tasks  # noqa: SLF001
        finally:
            runtime.release()
            await supervisor.aclose()
        assert slot not in supervisor._all_slots  # noqa: SLF001

    asyncio.run(exercise())


def test_round6_cli_config_edit_and_standalone_reconnect_boundary(
    tmp_path: Path,
) -> None:
    parser = build_parser()
    workspace = str(tmp_path)
    add = parser.parse_args(
        [
            "mcp",
            "add",
            "local_fixture",
            "--workspace",
            workspace,
            "--stdio-command",
            sys.executable,
            "--arg",
            str(FIXTURE),
        ]
    )
    result = asyncio.run(_mcp_command(add))
    assert result["status"] == "ok"
    listing = asyncio.run(
        _mcp_command(
            parser.parse_args(["mcp", "list", "--workspace", workspace])
        )
    )
    configured = next(
        item
        for item in listing["servers"]
        if item["server_id"] == "local_fixture"
    )
    assert configured["transport"] == "stdio"
    assert "endpoint" not in configured
    disabled = asyncio.run(
        _mcp_command(
            parser.parse_args(
                ["mcp", "disable", "local_fixture", "--workspace", workspace]
            )
        )
    )
    assert disabled["status"] == "ok"
    with pytest.raises(RuntimeError, match="active Host-owned supervisor"):
        asyncio.run(
            _mcp_command(
                parser.parse_args(
                    [
                        "mcp",
                        "reconnect",
                        "local_fixture",
                        "--workspace",
                        workspace,
                    ]
                )
            )
        )


def test_round6_config_is_closed_whole_entry_and_secret_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = tmp_path / "user-mcp.yaml"
    workspace = tmp_path / "workspace"
    (workspace / ".pulsara").mkdir(parents=True)
    user.write_text(
        "servers:\n"
        "  shared:\n"
        "    enabled: true\n"
        "    transport:\n"
        "      type: streamable_http\n"
        "      endpoint: https://example.invalid/mcp\n"
        "    auth:\n"
        "      type: bearer_environment_ref\n"
        "      environment_variable: ROUND6_SECRET\n",
        encoding="utf-8",
    )
    (workspace / ".pulsara" / "mcp.yaml").write_text(
        "servers:\n"
        "  shared:\n"
        "    enabled: true\n"
        "    transport:\n"
        "      type: stdio\n"
        f"      command: {json.dumps(sys.executable)}\n"
        f"      args: [{json.dumps(str(FIXTURE))}]\n"
        "    effect_policy:\n"
        "      default_effect: read_only\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ROUND6_SECRET", "must-not-appear")
    (resolved,) = load_mcp_server_configs(
        workspace_root=workspace,
        user_config_path=user,
        trust_workspace_config=True,
    )
    assert resolved.server_id == "shared"
    assert resolved.effect_policy.default_effect is McpConfiguredEffect.READ_ONLY
    assert "must-not-appear" not in repr(resolved)
    assert "must-not-appear" not in resolved.runtime_config_fingerprint

    untrusted_workspace = tmp_path / "untrusted-workspace"
    (untrusted_workspace / ".pulsara").mkdir(parents=True)
    (untrusted_workspace / ".pulsara" / "mcp.yaml").write_text(
        "servers:\n"
        "  repository_owned:\n"
        "    enabled: true\n"
        "    transport:\n"
        "      type: stdio\n"
        f"      command: {json.dumps(sys.executable)}\n"
        f"      args: [{json.dumps(str(FIXTURE))}]\n",
        encoding="utf-8",
    )
    (untrusted,) = load_mcp_server_configs(
        workspace_root=untrusted_workspace,
        user_config_path=tmp_path / "missing-user-mcp.yaml",
    )
    assert untrusted.server_id == "repository_owned"
    assert not untrusted.enabled
    (explicitly_trusted,) = load_mcp_server_configs(
        workspace_root=untrusted_workspace,
        user_config_path=tmp_path / "missing-user-mcp.yaml",
        trust_workspace_config=True,
    )
    assert explicitly_trusted.enabled

    secret_config = tmp_path / "secret-mcp.yaml"
    secret_config.write_text(
        "servers:\n"
        "  secret:\n"
        "    enabled: true\n"
        "    transport:\n"
        "      type: streamable_http\n"
        "      endpoint: https://example.invalid/mcp\n"
        "    auth:\n"
        "      type: bearer_environment_ref\n"
        "      environment_variable: ROUND6_SECRET\n",
        encoding="utf-8",
    )
    (secret_one,) = load_mcp_server_configs(user_config_path=secret_config)
    monkeypatch.setenv("ROUND6_SECRET", "rotated-must-not-appear")
    (secret_two,) = load_mcp_server_configs(user_config_path=secret_config)
    assert (
        secret_one.semantic_config_fingerprint
        == secret_two.semantic_config_fingerprint
    )
    assert (
        secret_one.runtime_config_fingerprint
        != secret_two.runtime_config_fingerprint
    )
    assert "must-not-appear" not in repr(secret_one)
    assert "rotated-must-not-appear" not in repr(secret_two)

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text(
        "servers:\n"
        "  invalid:\n"
        "    enabled: 'false'\n"
        "    transport:\n"
        "      type: stdio\n"
        "      command: python\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be a boolean"):
        load_mcp_server_configs(user_config_path=invalid)

    unknown = tmp_path / "unknown.yaml"
    unknown.write_text(
        "servers:\n"
        "  invalid:\n"
        "    enabled: true\n"
        "    transport:\n"
        "      type: stdio\n"
        "      command: python\n"
        "    surprise_policy: permissive\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown fields"):
        load_mcp_server_configs(user_config_path=unknown)


def test_round6_wire_bounds_and_result_type_presence_fail_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(McpWireBoundExceeded):
        bounded_json_loads(
            b'{"value":"oversized"}',
            maximum_bytes=4,
            maximum_nodes=32,
            maximum_depth=8,
        )
    with pytest.raises(McpWireBoundExceeded):
        bounded_json_loads(
            b'{"a":{"b":1}}',
            maximum_bytes=64,
            maximum_nodes=32,
            maximum_depth=2,
        )
    transport = _BoundedTransport(McpWireBounds())
    transport.enforce_closed_result_type = True
    for payload in (
        b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}',
        b'{"jsonrpc":"2.0","id":1,"result":'
        b'{"resultType":"future","tools":[]}}',
    ):
        with pytest.raises(
            McpProtocolConformanceError,
            match="RESULT_TYPE_CONFORMANCE",
        ):
            transport._decode(payload, maximum_bytes=1024)  # noqa: SLF001

    client = BoundedMcpSdkClient(
        _config(tmp_path),
        workspace_root=tmp_path,
        notification_callback=lambda _method: asyncio.sleep(0),
    )
    client._transport = transport  # noqa: SLF001
    complete_results = (
        types.CallToolResult(resultType="complete", content=[]),
        types.ReadResourceResult(resultType="complete", contents=[]),
        types.GetPromptResult(resultType="complete", messages=[]),
    )
    for result in complete_results:
        assert client.require_closed_result_type(result) == "complete"

    input_required = types.InputRequiredResult(
        resultType="input_required",
        inputRequests={},
        requestState="sealed",
    )
    assert client.require_closed_result_type(input_required) == "input_required"

    for missing in (
        types.CallToolResult(content=[]),
        types.ReadResourceResult(contents=[]),
        types.GetPromptResult(messages=[]),
    ):
        with pytest.raises(
            McpProtocolConformanceError,
            match="RESULT_TYPE_CONFORMANCE",
        ):
            client.require_closed_result_type(missing)

    for future in (
        types.CallToolResult(resultType="future", content=[]),
        types.ReadResourceResult(resultType="future", contents=[]),
        types.GetPromptResult(resultType="future", messages=[]),
    ):
        with pytest.raises(
            McpProtocolConformanceError,
            match="RESULT_TYPE_CONFORMANCE",
        ):
            client.require_closed_result_type(future)

    contradiction = types.CallToolResult(
        resultType="input_required",
        content=[],
    )
    with pytest.raises(
        McpProtocolConformanceError,
        match="PAYLOAD_CONTRADICTION",
    ):
        client.require_closed_result_type(contradiction)

    maximum = DEFAULT_MCP_WIRE_BOUNDS.maximum_schema_utf8_bytes
    validate_schema(
        {"type": "object", "description": "x" * (maximum - 64)},
        DEFAULT_MCP_WIRE_BOUNDS,
    )
    with pytest.raises(McpWireBoundExceeded):
        validate_schema(
            {"type": "object", "description": "x" * maximum},
            DEFAULT_MCP_WIRE_BOUNDS,
        )


def test_round6_does_not_expand_durable_or_protocol_oracles() -> None:
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 31
    assert len(LIVE_EVENT_TYPES) == 24
    assert len(SUBJECT_SLOTS) == 13
    assert len(APPEND_GUARDS) == 2
    assert len(CONVERSATION_KERNEL_RELATIONS) == 25
    assert len(JOB_HANDLER_CATALOG) == 1

    root = Path(__file__).parents[1]
    mcp_root = root / "src" / "pulsara_agent" / "conversation_kernel" / "mcp"
    forbidden = {
        "checkpoint",
        "receipt",
        "reducer",
        "projection",
        "event_log",
        "runtime_session",
    }
    sdk_importers: list[str] = []
    forbidden_identifiers = {
        "receipt",
        "checkpoint",
        "reducer",
        "repair",
        "reconciliation",
    }
    for path in mcp_root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not any(
            token in imported
            for imported in imports
            for token in forbidden
        ), path
        if any(
            imported in {"mcp", "mcp_types"}
            or imported.startswith("mcp.")
            for imported in imports
        ):
            sdk_importers.append(path.name)
            assert path.name == "sdk_facade.py"
        assert not any(
            token in node.name.lower()
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            for token in forbidden_identifiers
        ), path
        assert not any(
            imported.startswith("psycopg")
            or "storage.migrations" in imported
            or imported.endswith("conversation_kernel.repository")
            for imported in imports
        ), path
    assert sdk_importers == ["sdk_facade.py"]

    direct_model_source = (
        root
        / "src"
        / "pulsara_agent"
        / "conversation_kernel"
        / "direct_model.py"
    ).read_text(encoding="utf-8")
    assert '"execution_surface"' in direct_model_source

    assert {item.name for item in fields(FrozenToolSpec)} == {
        "name",
        "description",
        "parameters",
        "descriptor_fingerprint",
    }
    assert {item.value for item in McpEffectKind} == {
        "READ_ONLY",
        "EXTERNAL_EFFECT",
    }
    resource_schema = thaw_json(
        builtin_tool_catalog_entry("read_mcp_resource").descriptor.input_schema
    )
    assert isinstance(resource_schema, dict)
    properties = resource_schema.get("properties")
    assert isinstance(properties, dict)
    assert "offset" not in properties
    assert "limit" not in properties
    assert properties["uri"]["maxLength"] == 32768

    assert not any(
        "Mcp" in descriptor.event_type.value
        or "MCP" in descriptor.event_type.value
        for descriptor in COMMITTED_EVENT_DESCRIPTORS
    )
    assert not any("mcp" in relation.lower() for relation in CONVERSATION_KERNEL_RELATIONS)
    assert not any(
        "mcp" in contract.handler_type.lower()
        for contract in JOB_HANDLER_CATALOG
    )
    baseline = (
        root
        / "src"
        / "pulsara_agent"
        / "storage"
        / "migrations"
        / "sql"
        / "0000_conversation_kernel_baseline.sql"
    ).read_text(encoding="utf-8")
    # The clean-v0 interaction table already reserved MCP_INPUT as a dormant
    # subject kind before Round 6.  V1 must not add a new relation/generation
    # or activate that durable subject path.
    assert "mcp_generation" not in baseline.lower()
    kernel = root / "src" / "pulsara_agent" / "conversation_kernel"
    repository_paths = [kernel / "repository.py"]
    repository_paths.extend(sorted((kernel / "_repository").glob("*.py")))
    repository_source = "\n".join(
        path.read_text(encoding="utf-8") for path in repository_paths
    )
    assert "MCP_INPUT" not in repository_source
