"""Stable SDK-backed MCP v2 manager.

This module is the only production path that imports the official MCP SDK.
Everything above it speaks Pulsara-owned DTOs so SDK churn stays contained here.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import SplitResult, urlsplit, urlunsplit
from uuid import uuid4

import httpx2
import mcp.types as types
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from pydantic import TypeAdapter

from pulsara_agent.primitives._context_base import (
    FrozenJsonObjectFact,
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.runtime.mcp.manager import McpClientManager
from pulsara_agent.primitives.mcp import McpServerLifecycleTimingFact
from pulsara_agent.primitives.mcp_continuation import (
    default_mcp_continuation_bounds,
)
from pulsara_agent.primitives.mcp_protocol import (
    McpAuthAttributionFact,
    McpCachePageAttributionFact,
    McpCacheableMethod,
    McpClientCapabilityPolicyFact,
    McpClientInputMethod,
    McpDiscoveryAttributionFact,
    McpDiscoveryPageSetAttributionFact,
    McpEndpointAttributionFact,
    McpElicitationMode,
    McpFinalDiscoverWireReceiptFact,
    McpLegacyInitializeWireReceiptFact,
    McpPromptArgumentSemanticFact,
    McpPromptSemanticFact,
    McpProtocolBehaviorEra,
    McpProtocolNegotiationAttributionFact,
    McpServerProtocolSemanticFact,
    McpServerSnapshotAuthorityFact,
    McpServerSurfaceSemanticFact,
    McpToolDiscoveryAttributionFact,
    McpToolDiscoveryRejectionFact,
    McpResourceSemanticFact,
    McpResourceTemplateSemanticFact,
    behavior_era_for_protocol_revision,
    build_mcp_protocol_fact,
)
from pulsara_agent.runtime.mcp.schema import (
    MCP_OUTPUT_VALIDATION_CONTRACT_FINGERPRINT,
    MCP_SDK_CONFORMANCE_CONTRACT_FINGERPRINT,
    build_conformed_tool_schema,
    McpOutputSchemaMismatch,
    McpSchemaContractError,
    validate_structured_tool_result,
)
from pulsara_agent.runtime.mcp.contracts import (
    McpBindingDispatchBorrow,
    McpFreshnessRevalidationReceipt,
    McpSdkConcurrencyMode,
    McpSdkConformedClientGeneration,
    McpSdkNegotiatedProtocolBinding,
    McpSdkProtocolBinding,
    build_mcp_binding_dispatch_borrow,
    build_mcp_freshness_revalidation_receipt,
    build_raw_tool_call_result_carrier,
)
from pulsara_agent.ports.mcp import McpConfirmedContinuationDispatchReceipt
from pulsara_agent.ports.mcp_secret import (
    McpContinuationSecretBorrowIssuer,
    McpReplayReadyCarrierPlaintext,
    McpRetryableRequestPayload,
    build_retryable_prompt_get_payload,
    build_retryable_resource_read_payload,
    build_retryable_tool_call_payload,
    McpSecretAccessPurpose,
)
from pulsara_agent.runtime.mcp.protocol import (
    MCP_INPUT_REQUIRED_MAX_LEGS,
    McpClientInputRequired,
    McpClientInputRuntimeBinding,
    McpStateOnlyRetryLeg,
    lower_input_required_result,
    state_only_retry_delay,
)
from pulsara_agent.runtime.mcp.types import (
    McpContentArtifact,
    McpDrainError,
    McpDiscoveredPrompt,
    McpDiscoveredResource,
    McpDiscoveredResourceTemplate,
    McpDiscoveredTool,
    McpManagerLease,
    McpServerConfig,
    McpServerSnapshot,
    McpServerStatus,
    McpStdioConfig,
    McpStreamableHttpConfig,
    McpToolAnnotations,
    McpToolResult,
    event_safe_mcp_config_fingerprint,
    filter_mcp_tools,
    snapshot_semantic_fingerprint,
    runtime_mcp_secret_commitment,
)
from pulsara_agent.runtime.mcp.telemetry import (
    inject_mcp_trace_headers_safely,
    mcp_operation_trace_scope,
)
from pulsara_agent.runtime.mcp.subscriptions import (
    McpServerDirtySignal,
    McpSnapshotDirtyReason,
    McpSubscriptionDirtyKind,
)


DEFAULT_MCP_MAX_PAGES = 20
DEFAULT_MCP_MAX_ITEMS = 2_000
DEFAULT_MCP_INPUT_REQUIRED_TIMEOUT_SECONDS = 300.0
_CALL_TOOL_RESULT_ADAPTER = TypeAdapter(types.CallToolResult | types.InputRequiredResult)
_READ_RESOURCE_RESULT_ADAPTER = TypeAdapter(
    types.ReadResourceResult | types.InputRequiredResult
)
_GET_PROMPT_RESULT_ADAPTER = TypeAdapter(types.GetPromptResult | types.InputRequiredResult)

_SAFE_AMBIENT_ENV = {
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TMPDIR",
    "USER",
}


@dataclass(slots=True)
class _SdkServerConnection:
    config: McpServerConfig
    client: Client
    http_client: httpx2.AsyncClient | None = None
    close_requested: asyncio.Event | None = None
    owner_task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sdk_client_generation_id: str = field(
        default_factory=lambda: f"mcp_sdk_generation:{uuid4().hex}"
    )
    stateless_semaphore: asyncio.Semaphore = field(init=False, repr=False)
    freshness_reconcile_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )
    endpoint_attribution: McpEndpointAttributionFact | None = None
    auth_attribution: McpAuthAttributionFact | None = None
    capability_policy: McpClientCapabilityPolicyFact | None = None
    client_input_binding: McpClientInputRuntimeBinding | None = field(
        default=None,
        repr=False,
    )
    protocol_binding: (
        McpSdkNegotiatedProtocolBinding | McpSdkProtocolBinding | None
    ) = None
    client_generation: McpSdkConformedClientGeneration | None = field(
        default=None,
        repr=False,
    )
    negotiation_attribution: McpProtocolNegotiationAttributionFact | None = None
    snapshot_dirty_reasons: set[McpSnapshotDirtyReason] = field(
        default_factory=set,
        repr=False,
    )
    subscription_dirty_kinds: set[McpSubscriptionDirtyKind] = field(
        default_factory=set,
        repr=False,
    )
    dirty_callback: Callable[[McpServerDirtySignal], None] | None = field(
        default=None,
        repr=False,
    )
    subscription_task: asyncio.Task[None] | None = field(default=None, repr=False)
    freshness_deadline_monotonic: float | None = field(default=None, repr=False)
    admitted_operation_count: int = field(default=0, repr=False)
    freshness_generation: int = field(default=0, repr=False)
    snapshot_id: str | None = field(default=None, repr=False)
    snapshot_semantic_fingerprint: str | None = field(default=None, repr=False)
    config_epoch: int | None = field(default=None, repr=False)
    discovery_generation: int | None = field(default=None, repr=False)
    transport_generation: int | None = field(default=None, repr=False)
    dirty_signal_generation: int = field(default=0, repr=False)
    dispatch_state_lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.stateless_semaphore = asyncio.Semaphore(
            self.config.stateless_max_in_flight
        )


class _SdkOwnerStartError(RuntimeError):
    def __init__(
        self,
        cause: BaseException,
        *,
        close_requested: asyncio.Event,
        owner_task: asyncio.Task[None],
    ) -> None:
        self.cause = cause
        self.close_requested = close_requested
        self.owner_task = owner_task
        super().__init__(str(cause))


@dataclass(frozen=True, slots=True)
class _SdkPageItem:
    value: object
    source_page: McpCachePageAttributionFact


@dataclass(frozen=True, slots=True)
class _SdkListingCapture:
    items: tuple[_SdkPageItem, ...]
    page_set: McpDiscoveryPageSetAttributionFact
    freshness_deadline_monotonic: float | None


class SdkMcpConnectError(RuntimeError):
    """Connect failure whose partially started SDK owner still needs drain."""

    def __init__(self, connection: "SdkMcpConnection", cause: BaseException) -> None:
        self.connection = connection
        self.cause = cause
        super().__init__(f"{type(cause).__name__}: {cause}")


class SdkMcpConnectCancelled(asyncio.CancelledError):
    """Caller cancellation carrying the partial SDK connection cleanup owner."""

    def __init__(self, connection: "SdkMcpConnection") -> None:
        self.connection = connection
        super().__init__("MCP SDK connect cancelled")


class McpSnapshotReconcileRequired(RuntimeError):
    def __init__(self, server_id: str, reasons: tuple[str, ...]) -> None:
        self.server_id = server_id
        self.reasons = reasons
        super().__init__(
            f"MCP snapshot requires reconcile before dispatch: {server_id} "
            f"({', '.join(reasons)})"
        )


class McpUnsupportedResultTypeError(RuntimeError):
    """The SDK decoded a result discriminator Pulsara does not own."""

    def __init__(self, *, method: str, result_type: object) -> None:
        self.method = method
        self.result_type = result_type
        super().__init__(
            f"MCP_UNSUPPORTED_RESULT_TYPE: {method} returned {result_type!r}"
        )


@dataclass(slots=True)
class _McpDispatchFreshnessPermit:
    """One-shot proof that a zero-TTL listing was just revalidated."""

    receipt: McpFreshnessRevalidationReceipt
    dispatch_operation_id: str
    freshness_generation: int
    allow_expired_deadline_once: bool
    _consumed: bool = field(default=False, init=False, repr=False)

    def require_current(
        self,
        *,
        server_id: str,
        snapshot_id: str,
        snapshot_semantic_fingerprint: str,
        snapshot_authority_fingerprint: str,
        sdk_client_generation_id: str,
        freshness_generation: int,
        operation_id: str,
    ) -> None:
        receipt = self.receipt
        if self._consumed:
            raise RuntimeError("MCP freshness permit was already consumed")
        if (
            receipt.server_id != server_id
            or receipt.installed_snapshot_id != snapshot_id
            or receipt.installed_snapshot_semantic_fingerprint
            != snapshot_semantic_fingerprint
            or receipt.installed_snapshot_authority_fingerprint
            != snapshot_authority_fingerprint
            or receipt.sdk_client_generation_id != sdk_client_generation_id
            or self.dispatch_operation_id != operation_id
            or self.freshness_generation <= 0
            or self.freshness_generation > freshness_generation
        ):
            raise RuntimeError("MCP freshness permit authority drifted")

    def consume(self) -> None:
        if self._consumed:
            raise RuntimeError("MCP freshness permit was already consumed")
        self._consumed = True


def _mcp_sdk_concurrency_mode(
    connection: _SdkServerConnection,
) -> McpSdkConcurrencyMode:
    binding = connection.protocol_binding
    if binding is None:
        raise RuntimeError("MCP concurrency mode requires negotiated protocol")
    transport = connection.config.transport
    if isinstance(transport, McpStdioConfig):
        return McpSdkConcurrencyMode.SERIALIZED
    if not isinstance(transport, McpStreamableHttpConfig):
        raise TypeError(f"unsupported MCP transport: {type(transport).__name__}")
    if (
        binding.protocol_semantic.behavior_era
        is McpProtocolBehaviorEra.STATELESS_PER_REQUEST
    ):
        return McpSdkConcurrencyMode.BOUNDED_PARALLEL
    return McpSdkConcurrencyMode.SERIALIZED


async def _run_sdk_discovery_operations(
    connection: _SdkServerConnection,
    operations: tuple[
        tuple[str, Callable[[], Awaitable[_SdkListingCapture]]],
        ...,
    ],
) -> dict[str, _SdkListingCapture]:
    mode = _mcp_sdk_concurrency_mode(connection)
    if mode is McpSdkConcurrencyMode.SERIALIZED:
        captures: dict[str, _SdkListingCapture] = {}
        async with connection.lock:
            for name, operation in operations:
                captures[name] = await operation()
        return captures

    async def run_bounded(
        operation: Callable[[], Awaitable[_SdkListingCapture]],
    ) -> _SdkListingCapture:
        async with connection.stateless_semaphore:
            return await operation()

    tasks: dict[str, asyncio.Task[_SdkListingCapture]] = {}
    async with asyncio.TaskGroup() as task_group:
        for name, operation in operations:
            tasks[name] = task_group.create_task(run_bounded(operation))
    return {name: task.result() for name, task in tasks.items()}


@dataclass(slots=True)
class SdkMcpConnection:
    """Connected, not-yet-discovered per-server SDK owner."""

    _connection: _SdkServerConnection
    _closed: bool = False

    @classmethod
    async def connect(
        cls,
        config: McpServerConfig,
        *,
        timeout_seconds: float,
        client_input_binding: McpClientInputRuntimeBinding | None = None,
    ) -> "SdkMcpConnection":
        deadline_monotonic = time.monotonic() + timeout_seconds
        client, http_client = _build_sdk_client(
            config,
            client_input_binding=client_input_binding,
        )
        try:
            close_requested, owner_task = await _start_sdk_client_owner(
                client,
                timeout_seconds=timeout_seconds,
            )
        except _SdkOwnerStartError as exc:
            connection = cls(
                _SdkServerConnection(
                    config=config,
                    client=client,
                    http_client=http_client,
                    close_requested=exc.close_requested,
                    owner_task=exc.owner_task,
                    client_input_binding=client_input_binding,
                )
            )
            if isinstance(exc.cause, asyncio.CancelledError):
                raise SdkMcpConnectCancelled(connection) from exc.cause
            raise SdkMcpConnectError(connection, exc.cause) from exc.cause
        except BaseException:
            if http_client is not None:
                await _best_effort_sdk_close_step(http_client.aclose())
            raise
        connection = cls(
            _SdkServerConnection(
                config=config,
                client=client,
                http_client=http_client,
                close_requested=close_requested,
                owner_task=owner_task,
                client_input_binding=client_input_binding,
            )
        )
        try:
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("MCP negotiation deadline expired")
            await asyncio.wait_for(
                _install_stable_negotiation_authority(connection._connection),
                timeout=remaining,
            )
        except asyncio.CancelledError as exc:
            raise SdkMcpConnectCancelled(connection) from exc
        except BaseException as exc:
            raise SdkMcpConnectError(connection, exc) from exc
        return connection

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        if self._closed:
            return
        await _close_sdk_connection(
            self._connection,
            timeout_seconds=timeout_seconds,
        )
        self._closed = True


@dataclass(slots=True)
class SdkMcpClientManager(McpClientManager):
    """Session-owned official Python MCP SDK v2 manager."""

    _snapshots: tuple[McpServerSnapshot, ...]
    _connections: dict[str, _SdkServerConnection]
    max_pages: int = DEFAULT_MCP_MAX_PAGES
    max_items: int = DEFAULT_MCP_MAX_ITEMS
    _closed: bool = False
    _active_tasks: set[asyncio.Task[Any]] = field(default_factory=set, init=False, repr=False)
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _wire_borrows: McpContinuationSecretBorrowIssuer = field(
        default_factory=lambda: McpContinuationSecretBorrowIssuer(
            f"mcp-sdk-wire:{uuid4().hex}"
        ),
        init=False,
        repr=False,
    )

    @classmethod
    def from_connected_server(
        cls,
        *,
        connection: SdkMcpConnection,
        snapshot: McpServerSnapshot,
        max_pages: int = DEFAULT_MCP_MAX_PAGES,
        max_items: int = DEFAULT_MCP_MAX_ITEMS,
        dirty_callback: Callable[[McpServerDirtySignal], None] | None = None,
    ) -> "SdkMcpClientManager":
        raw = connection._connection
        connection._closed = True  # ownership moves into the manager
        binding = raw.protocol_binding
        generation = raw.client_generation
        if (
            not isinstance(binding, McpSdkProtocolBinding)
            or generation is None
        ):
            raise RuntimeError("MCP manager requires a complete client generation")
        if (
            generation.sdk_protocol_binding is not binding
            or generation.client is not raw.client
            or not generation.accepting_operations
        ):
            raise RuntimeError("MCP manager client generation authority drifted")
        authority = snapshot.authority
        if authority is None:
            raise RuntimeError("MCP manager requires snapshot authority")
        if (
            authority.discovery_attribution.negotiation.negotiation_wire_receipt_fingerprint
            != generation.final_negotiation_wire_receipt.receipt_fingerprint
            or authority.surface_semantic.protocol_semantic.semantic_fingerprint
            != binding.protocol_semantic.semantic_fingerprint
            or snapshot.snapshot_id != generation.snapshot_id
            or snapshot.snapshot_semantic_fingerprint
            != generation.snapshot_semantic_fingerprint
            or authority.authority_fingerprint
            != generation.snapshot_authority_fingerprint
        ):
            raise RuntimeError("MCP snapshot does not join its client generation")
        installed_attributions = tuple(
            sorted(
                tool.discovery_attribution.attribution_fingerprint
                for tool in snapshot.tools
                if tool.discovery_attribution is not None
            )
        )
        if not set(installed_attributions).issubset(
            generation.ordered_tool_attribution_fingerprints
        ):
            raise RuntimeError("MCP snapshot tool listing generation drifted")
        raw.snapshot_id = snapshot.snapshot_id
        raw.snapshot_semantic_fingerprint = snapshot.snapshot_semantic_fingerprint
        raw.config_epoch = snapshot.config_epoch
        raw.discovery_generation = snapshot.discovery_generation
        raw.transport_generation = binding.transport_generation
        raw.dirty_callback = dirty_callback
        manager = cls(
            _snapshots=(snapshot,),
            _connections={snapshot.server_id: raw},
            max_pages=max_pages,
            max_items=max_items,
        )
        return manager

    @property
    def snapshots(self) -> tuple[McpServerSnapshot, ...]:
        return self._snapshots

    def recovery_authority(self, server_id: str) -> tuple[str, str, str, str, str]:
        """Return the non-secret live authority used for continuation rebind."""

        connection = self._require_connection(server_id)
        binding = connection.protocol_binding
        endpoint = connection.endpoint_attribution
        auth = connection.auth_attribution
        if binding is None or endpoint is None or auth is None:
            raise RuntimeError("MCP recovery authority is not READY")
        return (
            binding.protocol_semantic.protocol_revision,
            binding.protocol_semantic.semantic_fingerprint,
            endpoint.attribution_fingerprint,
            auth.attribution_fingerprint,
            _safe_sdk_generation_id(connection),
        )

    def recovery_rebind_authority(
        self,
        server_id: str,
        snapshot_id: str,
    ) -> tuple[str, str, str, str, str, str]:
        base = self.recovery_authority(server_id)
        matches = tuple(
            item
            for item in self._snapshots
            if item.server_id == server_id and item.snapshot_id == snapshot_id
        )
        if len(matches) != 1 or matches[0].status is not McpServerStatus.READY:
            raise RuntimeError("MCP recovery snapshot authority is unavailable")
        return (*base, matches[0].snapshot_semantic_fingerprint)

    async def _discover_connected(
        self,
        connection: _SdkServerConnection,
        *,
        config_epoch: int,
        reconcile_attempt_id: str,
        discovery_generation: int,
        queued_at_utc: str,
        queued_monotonic: float,
        connect_started_at_utc: str,
        connect_ended_at_utc: str,
        connect_duration_seconds: float,
        discovery_started_at_utc: str,
        discovery_started_monotonic: float,
        install_client_generation: bool = False,
    ) -> tuple[McpServerSnapshot, int, int]:
        config = connection.config
        diagnostics: list[dict[str, Any]] = []
        metrics = {"request_count": 0, "page_count": 0}
        capabilities = getattr(connection.client, "server_capabilities", None)
        discovery_operations: list[
            tuple[str, Callable[[], Awaitable[_SdkListingCapture]]]
        ] = []
        strict_cache_hints = connection.protocol_binding is not None and (
            connection.protocol_binding.protocol_semantic.behavior_era
            is McpProtocolBehaviorEra.STATELESS_PER_REQUEST
        )
        if capabilities is not None and getattr(capabilities, "tools", None) is not None:
            discovery_operations.append(
                (
                    "tools",
                    lambda: self._list_all(
                        "tools/list",
                        McpCacheableMethod.TOOLS_LIST,
                        lambda cursor: connection.client.session.list_tools(
                            params=types.PaginatedRequestParams(cursor=cursor)
                        ),
                        diagnostics,
                        item_attr="tools",
                        metrics=metrics,
                        strict_cache_hints=strict_cache_hints,
                    ),
                )
            )
        if capabilities is not None and getattr(capabilities, "resources", None) is not None:
            discovery_operations.extend(
                (
                    (
                        "resources",
                        lambda: self._list_all(
                            "resources/list",
                            McpCacheableMethod.RESOURCES_LIST,
                            lambda cursor: connection.client.session.list_resources(
                                params=types.PaginatedRequestParams(cursor=cursor)
                            ),
                            diagnostics,
                            item_attr="resources",
                            metrics=metrics,
                            strict_cache_hints=strict_cache_hints,
                        ),
                    ),
                    (
                        "resource_templates",
                        lambda: self._list_all(
                            "resources/templates/list",
                            McpCacheableMethod.RESOURCE_TEMPLATES_LIST,
                            lambda cursor: (
                                connection.client.session.list_resource_templates(
                                    params=types.PaginatedRequestParams(cursor=cursor)
                                )
                            ),
                            diagnostics,
                            item_attr="resource_templates",
                            metrics=metrics,
                            strict_cache_hints=strict_cache_hints,
                        ),
                    ),
                )
            )
        if capabilities is not None and getattr(capabilities, "prompts", None) is not None:
            discovery_operations.append(
                (
                    "prompts",
                    lambda: self._list_all(
                        "prompts/list",
                        McpCacheableMethod.PROMPTS_LIST,
                        lambda cursor: connection.client.session.list_prompts(
                            params=types.PaginatedRequestParams(cursor=cursor)
                        ),
                        diagnostics,
                        item_attr="prompts",
                        metrics=metrics,
                        strict_cache_hints=strict_cache_hints,
                    ),
                )
            )
        captures = await _run_sdk_discovery_operations(
            connection,
            tuple(discovery_operations),
        )
        freshness_deadlines = tuple(
            capture.freshness_deadline_monotonic
            for capture in captures.values()
            if capture.freshness_deadline_monotonic is not None
        )
        connection.freshness_deadline_monotonic = (
            min(freshness_deadlines) if freshness_deadlines else None
        )
        ended_monotonic = time.monotonic()
        ended_at_utc = _utc_now()
        tool_capture = captures.get("tools")
        listing_generation_fingerprint = context_fingerprint(
            "mcp-sdk-conformed-listing-generation:v1",
            {
                "sdk_client_generation_id": connection.sdk_client_generation_id,
                "page_set_fingerprint": (
                    tool_capture.page_set.page_set_fingerprint
                    if tool_capture is not None
                    else None
                ),
            },
        )
        all_tool_facts_list: list[McpDiscoveredTool] = []
        tool_rejections: list[McpToolDiscoveryRejectionFact] = []
        for item in (tool_capture.items if tool_capture is not None else ()):
            try:
                all_tool_facts_list.append(
                    _tool_from_sdk(
                        config.server_id,
                        item.value,
                        source_page_receipt_fingerprint=(
                            item.source_page.page_receipt_fingerprint
                        ),
                        listing_generation_fingerprint=(
                            listing_generation_fingerprint
                        ),
                    )
                )
            except McpSchemaContractError as exc:
                raw_tool = _sdk_result_payload(item.value)
                observed_name = str(getattr(item.value, "name", "<unnamed>"))
                rejection = build_mcp_protocol_fact(
                    McpToolDiscoveryRejectionFact,
                    schema_version="mcp_tool_discovery_rejection.v1",
                    server_id=config.server_id,
                    observed_tool_name=observed_name,
                    source_page_receipt_fingerprint=(
                        item.source_page.page_receipt_fingerprint
                    ),
                    observed_tool_payload_fingerprint=context_fingerprint(
                        "mcp-rejected-tool-payload:v1",
                        raw_tool,
                    ),
                    reason_code=exc.code,
                    sdk_conformed_listing_generation_fingerprint=(
                        listing_generation_fingerprint
                    ),
                )
                tool_rejections.append(rejection)
                diagnostics.append(
                    {
                        "code": "mcp_tool_schema_rejected",
                        "tool_name": observed_name,
                        "reason_code": exc.code.value,
                    }
                )
        all_tool_facts = tuple(all_tool_facts_list)
        tool_facts = filter_mcp_tools(
            config,
            all_tool_facts,
        )
        resource_facts = tuple(
            _resource_from_sdk(config.server_id, item.value)
            for item in (
                captures["resources"].items if "resources" in captures else ()
            )
        )
        template_facts = tuple(
            _resource_template_from_sdk(config.server_id, item.value)
            for item in (
                captures["resource_templates"].items
                if "resource_templates" in captures
                else ()
            )
        )
        prompt_facts = tuple(
            _prompt_from_sdk(config.server_id, item.value)
            for item in (captures["prompts"].items if "prompts" in captures else ())
        )
        server_info_model = connection.client.server_info
        server_info = (
            server_info_model.model_dump(mode="json", by_alias=True)
            if server_info_model is not None
            else {}
        )
        preliminary_semantic_fingerprint = snapshot_semantic_fingerprint(
            server_id=config.server_id,
            status=McpServerStatus.READY,
            tools=tool_facts,
            resources=resource_facts,
            resource_templates=template_facts,
            prompts=prompt_facts,
            protocol_version=connection.client.protocol_version,
            server_info=server_info,
            instructions=connection.client.instructions,
        )
        protocol_binding = connection.protocol_binding
        endpoint = connection.endpoint_attribution
        auth = connection.auth_attribution
        if protocol_binding is None or endpoint is None or auth is None:
            raise RuntimeError("MCP snapshot identity lacks negotiation authority")
        snapshot_id = "mcp_snapshot:" + context_fingerprint(
            "mcp-server-snapshot-identity:v2",
            {
                "server_id": config.server_id,
                "config_epoch": config_epoch,
                "event_safe_config_fingerprint": (
                    event_safe_mcp_config_fingerprint(config)
                ),
                "discovery_generation": discovery_generation,
                "surface_semantic_fingerprint": preliminary_semantic_fingerprint,
                "protocol_semantic_fingerprint": (
                    protocol_binding.protocol_semantic.semantic_fingerprint
                ),
                "endpoint_attribution_fingerprint": endpoint.attribution_fingerprint,
                "auth_attribution_fingerprint": auth.attribution_fingerprint,
            },
        ).removeprefix("sha256:")
        authority = _build_snapshot_authority(
            connection=connection,
            snapshot_id=snapshot_id,
            config_epoch=config_epoch,
            discovery_generation=discovery_generation,
            reconcile_attempt_id=reconcile_attempt_id,
            captures=captures,
            tools=all_tool_facts,
            resources=resource_facts,
            resource_templates=template_facts,
            prompts=prompt_facts,
            instructions=connection.client.instructions,
            tool_rejections=tuple(
                sorted(tool_rejections, key=lambda item: item.observed_tool_name)
            ),
        )
        semantic_fingerprint = (
            authority.surface_semantic_fingerprint
            if authority is not None
            else preliminary_semantic_fingerprint
        )
        snapshot = McpServerSnapshot(
            snapshot_id=snapshot_id,
            server_id=config.server_id,
            config_epoch=config_epoch,
            event_safe_config_fingerprint=event_safe_mcp_config_fingerprint(config),
            snapshot_semantic_fingerprint=semantic_fingerprint,
            reconcile_attempt_id=reconcile_attempt_id,
            discovery_generation=discovery_generation,
            status=McpServerStatus.READY,
            required=config.required,
            tools=tool_facts,
            resources=resource_facts,
            resource_templates=template_facts,
            prompts=prompt_facts,
            protocol_version=connection.client.protocol_version,
            server_info=server_info,
            instructions=connection.client.instructions,
            diagnostics=tuple(diagnostics),
            authority=authority,
            timing=McpServerLifecycleTimingFact(
                queued_at_utc=queued_at_utc,
                connect_started_at_utc=connect_started_at_utc,
                connect_ended_at_utc=connect_ended_at_utc,
                discovery_started_at_utc=discovery_started_at_utc,
                discovery_ended_at_utc=ended_at_utc,
                completed_at_utc=ended_at_utc,
                connect_duration_seconds=connect_duration_seconds,
                discovery_duration_seconds=max(0.0, ended_monotonic - discovery_started_monotonic),
                total_duration_seconds=max(0.0, ended_monotonic - queued_monotonic),
            ),
        )
        if install_client_generation:
            _install_conformed_client_generation(
                connection=connection,
                snapshot=snapshot,
                tool_capture=tool_capture,
                all_tool_facts=all_tool_facts,
                tool_rejections=tuple(tool_rejections),
            )
        return snapshot, metrics["request_count"], metrics["page_count"]

    async def _list_all(
        self,
        method: str,
        cache_method: McpCacheableMethod,
        fetch: Callable[[str | None], Any],
        diagnostics: list[dict[str, Any]],
        *,
        item_attr: str,
        metrics: dict[str, int],
        strict_cache_hints: bool,
    ) -> _SdkListingCapture:
        items: list[_SdkPageItem] = []
        pages: list[McpCachePageAttributionFact] = []
        freshness_deadlines: list[float] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for page_ordinal in range(self.max_pages):
            metrics["request_count"] += 1
            metrics["page_count"] += 1
            result = await fetch(cursor)
            received_monotonic = time.monotonic()
            ttl_present = _sdk_wire_field_present(
                result,
                field_name="ttl_ms",
                alias="ttlMs",
            )
            scope_present = _sdk_wire_field_present(
                result,
                field_name="cache_scope",
                alias="cacheScope",
            )
            raw_ttl_ms = getattr(result, "ttl_ms", None) if ttl_present else None
            raw_cache_scope = (
                getattr(result, "cache_scope", None) if scope_present else None
            )
            if strict_cache_hints and (
                not ttl_present
                or not scope_present
                or raw_ttl_ms is None
                or raw_cache_scope is None
            ):
                raise RuntimeError(
                    f"MCP_CACHE_HINT_INVALID: {method} omitted cache hints"
                )
            next_cursor = getattr(result, "next_cursor", None)
            raw_result = _sdk_result_payload(result)
            page_fact = build_mcp_protocol_fact(
                McpCachePageAttributionFact,
                schema_version="mcp_cache_page_attribution.v1",
                method=cache_method,
                request_params_fingerprint=context_fingerprint(
                    "mcp-cache-page-request-params:v1",
                    {"method": method, "cursor": cursor},
                ),
                request_cursor=cursor,
                page_ordinal=page_ordinal,
                received_at_utc=_utc_now(),
                raw_ttl_ms=raw_ttl_ms,
                resolved_ttl_ms=max(0, int(raw_ttl_ms or 0)),
                raw_cache_scope=raw_cache_scope,
                resolved_cache_scope=(
                    raw_cache_scope if raw_cache_scope in {"public", "private"} else "private"
                ),
                hint_disposition=(
                    "negative_normalized"
                    if isinstance(raw_ttl_ms, int) and raw_ttl_ms < 0
                    else "exact"
                    if raw_ttl_ms is not None and raw_cache_scope is not None
                    else "absent_earlier_revision"
                ),
                result_payload_fingerprint=context_fingerprint(
                    "mcp-cache-page-result:v1",
                    raw_result,
                ),
                next_cursor=next_cursor,
            )
            pages.append(page_fact)
            if raw_ttl_ms is not None:
                freshness_deadlines.append(
                    received_monotonic + max(0, int(raw_ttl_ms)) / 1000
                )
            for item in getattr(result, item_attr):
                items.append(_SdkPageItem(value=item, source_page=page_fact))
            truncated = len(items) > self.max_items
            if truncated:
                raise RuntimeError(
                    "MCP discovery cannot install a partial listing: "
                    f"{method} exceeded {self.max_items} items"
                )
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise RuntimeError(f"repeated MCP pagination cursor for {method}")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise RuntimeError(
                f"MCP pagination exceeded max pages for {method}: {self.max_pages}"
            )
        scopes = {page.resolved_cache_scope for page in pages}
        if len(scopes) != 1:
            raise RuntimeError(f"{method} returned mixed cache scopes")
        common_scope = pages[0].resolved_cache_scope
        page_set = build_mcp_protocol_fact(
            McpDiscoveryPageSetAttributionFact,
            schema_version="mcp_discovery_page_set_attribution.v1",
            method=cache_method,
            started_from_cursor_none=True,
            ordered_pages=tuple(pages),
            page_receipt_accumulator=context_fingerprint(
                "mcp-discovery-page-receipt-accumulator:v1",
                tuple(page.page_receipt_fingerprint for page in pages),
            ),
            common_resolved_cache_scope=common_scope,
            complete_capture=pages[-1].next_cursor is None,
        )
        return _SdkListingCapture(
            items=tuple(items),
            page_set=page_set,
            freshness_deadline_monotonic=(
                min(freshness_deadlines) if freshness_deadlines else None
            ),
        )

    async def _drive_input_required_request(
        self,
        *,
        connection: _SdkServerConnection,
        binding_lease: McpManagerLease,
        request_factory: Callable[
            [dict[str, dict[str, Any]] | None, str | None],
            object,
        ],
        result_adapter: TypeAdapter[Any],
        complete_result_type: type[object],
        retryable_payload: McpRetryableRequestPayload,
        timeout_ms: int,
        input_responses: dict[str, dict[str, Any]] | None,
        request_state: str | None,
        leg_ordinal: int,
        interaction_id: str | None,
        trace_method: str,
        target_kind: str,
        target_semantic_fingerprint: str,
        operation_deadline_monotonic: float | None = None,
    ) -> tuple[object, str] | McpClientInputRequired:
        """Consume state-only legs and yield only a real human-input boundary."""

        request_deadline = time.monotonic() + timeout_ms / 1000
        if operation_deadline_monotonic is not None:
            request_deadline = min(
                request_deadline,
                operation_deadline_monotonic,
            )
        current_responses = input_responses
        current_state = request_state
        current_ordinal = leg_ordinal
        while True:
            remaining = request_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("MCP request deadline expired during MRTR")
            operation_id = f"mcp_operation:{uuid4().hex}"
            freshness_permit = await self._prepare_dispatch_freshness(
                connection,
                timeout_seconds=remaining,
                dispatch_operation_id=operation_id,
            )
            with mcp_operation_trace_scope(
                server_id=connection.config.server_id,
                method=trace_method,
            ):
                async with _mcp_operation_lane(
                    connection,
                    binding_lease=binding_lease,
                    operation_id=operation_id,
                    target_kind=target_kind,
                    target_semantic_fingerprint=target_semantic_fingerprint,
                    freshness_permit=freshness_permit,
                ) as dispatch_borrow:
                    dispatch_borrow.require_active(operation_id=operation_id)
                    result = await connection.client.session.send_request(
                        request_factory(current_responses, current_state),
                        result_adapter,
                        request_read_timeout_seconds=remaining,
                    )
            result_type = getattr(result, "result_type", None)
            if result_type == "complete":
                if not isinstance(result, complete_result_type):
                    raise McpUnsupportedResultTypeError(
                        method=trace_method,
                        result_type=type(result).__name__,
                    )
                return result, operation_id
            if result_type != "input_required" or not isinstance(
                result, types.InputRequiredResult
            ):
                raise McpUnsupportedResultTypeError(
                    method=trace_method,
                    result_type=result_type,
                )
            binding = connection.client_input_binding
            wire_requests = {
                str(key): request.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                )
                for key, request in (result.input_requests or {}).items()
            }
            continuation_bounds = default_mcp_continuation_bounds()
            leg, private_urls = lower_input_required_result(
                input_requests=wire_requests,
                request_state=result.request_state,
                leg_ordinal=current_ordinal,
                retryable_payload=retryable_payload,
                operation_deadline_monotonic=(
                    operation_deadline_monotonic
                    if operation_deadline_monotonic is not None
                    else time.monotonic()
                    + DEFAULT_MCP_INPUT_REQUIRED_TIMEOUT_SECONDS
                ),
                commitment_key_id=(
                    binding.commitment_key_id if binding is not None else "disabled"
                ),
                keyed_commitment=(
                    binding.keyed_commitment
                    if binding is not None
                    else _disabled_mcp_secret_commitment
                ),
                elicitation_advertised=(binding is not None),
                bounds=continuation_bounds,
            )
            if isinstance(leg, McpStateOnlyRetryLeg):
                if current_ordinal >= MCP_INPUT_REQUIRED_MAX_LEGS:
                    raise RuntimeError("MCP input-required rounds exceeded")
                delay = state_only_retry_delay(current_ordinal)
                if time.monotonic() + delay >= request_deadline:
                    raise TimeoutError("MCP state-only retry exceeded request deadline")
                await asyncio.sleep(delay)
                current_state = leg.request_state
                current_responses = None
                current_ordinal += 1
                continue
            protocol = connection.protocol_binding
            endpoint = connection.endpoint_attribution
            auth = connection.auth_attribution
            if protocol is None or endpoint is None or auth is None:
                raise RuntimeError("MCP input-required result lacks negotiation authority")
            return McpClientInputRequired(
                interaction_id=(
                    interaction_id or f"mcp_input_required:{uuid4().hex}"
                ),
                server_id=connection.config.server_id,
                exact_protocol_revision=protocol.protocol_semantic.protocol_revision,
                protocol_semantic_fingerprint=(
                    protocol.protocol_semantic.semantic_fingerprint
                ),
                endpoint_attribution_fingerprint=endpoint.attribution_fingerprint,
                auth_attribution_fingerprint=auth.attribution_fingerprint,
                sdk_client_generation_id=_safe_sdk_generation_id(connection),
                leg=leg,
                retryable_request_payload=retryable_payload,
                private_url_payloads=private_urls,
                continuation_bounds=continuation_bounds,
                first_input_required_observed_at_utc=_utc_now(),
            )

    async def _prepare_dispatch_freshness(
        self,
        connection: _SdkServerConnection,
        *,
        timeout_seconds: float,
        dispatch_operation_id: str,
    ) -> _McpDispatchFreshnessPermit | None:
        """Synchronously revalidate a TTL-stale, otherwise clean snapshot.

        A zero-TTL page is valid as the result of the discovery operation but
        cannot authorize a later physical request by itself.  We therefore
        perform one fresh, complete listing immediately before dispatch.  An
        exact no-change result grants only this operation a one-shot permit;
        any semantic change remains owned by the Supervisor safe-point path.
        """

        with connection.dispatch_state_lock:
            deadline = connection.freshness_deadline_monotonic
            if deadline is not None and time.monotonic() >= deadline:
                connection.snapshot_dirty_reasons.add(
                    McpSnapshotDirtyReason.TTL_EXPIRED
                )
            reasons = _dirty_reason_values(connection)
        if not reasons:
            return None
        if reasons != (McpSnapshotDirtyReason.TTL_EXPIRED.value,):
            raise McpSnapshotReconcileRequired(
                connection.config.server_id,
                reasons,
            )

        async with connection.freshness_reconcile_lock:
            with connection.dispatch_state_lock:
                deadline = connection.freshness_deadline_monotonic
                if deadline is not None and time.monotonic() >= deadline:
                    connection.snapshot_dirty_reasons.add(
                        McpSnapshotDirtyReason.TTL_EXPIRED
                    )
                reasons = _dirty_reason_values(connection)
            if not reasons:
                return None
            if reasons != (McpSnapshotDirtyReason.TTL_EXPIRED.value,):
                raise McpSnapshotReconcileRequired(
                    connection.config.server_id,
                    reasons,
                )

            current = self._snapshot_for_server(connection.config.server_id)
            client_generation = connection.client_generation
            current_authority = current.authority
            if (
                client_generation is None
                or current_authority is None
                or client_generation.snapshot_id != current.snapshot_id
                or client_generation.snapshot_semantic_fingerprint
                != current.snapshot_semantic_fingerprint
                or client_generation.snapshot_authority_fingerprint
                != current_authority.authority_fingerprint
            ):
                raise RuntimeError(
                    "MCP freshness revalidation lacks installed generation authority"
                )
            started = time.monotonic()
            now_utc = _utc_now()
            physical_operation_id = f"mcp_freshness_revalidation:{uuid4().hex}"
            try:
                refreshed, request_count, page_count = await asyncio.wait_for(
                    self._discover_connected(
                        connection,
                        config_epoch=current.config_epoch,
                        reconcile_attempt_id=current.reconcile_attempt_id,
                        discovery_generation=current.discovery_generation,
                        queued_at_utc=now_utc,
                        queued_monotonic=started,
                        connect_started_at_utc=now_utc,
                        connect_ended_at_utc=now_utc,
                        connect_duration_seconds=0.0,
                        discovery_started_at_utc=now_utc,
                        discovery_started_monotonic=started,
                    ),
                    timeout=max(
                        0.001,
                        min(
                            timeout_seconds,
                            connection.config.discovery_timeout_ms / 1000,
                        ),
                    ),
                )
            except BaseException:
                self._publish_dirty_signal(connection)
                raise

            if (
                refreshed.snapshot_id != current.snapshot_id
                or refreshed.snapshot_semantic_fingerprint
                != current.snapshot_semantic_fingerprint
                or refreshed.protocol_version != current.protocol_version
                or refreshed.event_safe_config_fingerprint
                != current.event_safe_config_fingerprint
            ):
                with connection.dispatch_state_lock:
                    connection.snapshot_dirty_reasons.add(
                        McpSnapshotDirtyReason.LIST_CHANGED
                    )
                self._publish_dirty_signal(connection)
                raise McpSnapshotReconcileRequired(
                    connection.config.server_id,
                    _dirty_reason_values(connection),
                )

            refreshed_authority = refreshed.authority
            if refreshed_authority is None:
                raise RuntimeError("MCP freshness revalidation lacks refreshed authority")
            receipt = build_mcp_freshness_revalidation_receipt(
                physical_operation_id=physical_operation_id,
                server_id=connection.config.server_id,
                sdk_client_generation_id=client_generation.generation_id,
                installed_snapshot_id=current.snapshot_id,
                installed_snapshot_semantic_fingerprint=(
                    current.snapshot_semantic_fingerprint
                ),
                installed_snapshot_authority_fingerprint=(
                    current_authority.authority_fingerprint
                ),
                refreshed_snapshot_id=refreshed.snapshot_id,
                refreshed_snapshot_semantic_fingerprint=(
                    refreshed.snapshot_semantic_fingerprint
                ),
                refreshed_snapshot_authority_fingerprint=(
                    refreshed_authority.authority_fingerprint
                ),
                refreshed_page_set_accumulator=context_fingerprint(
                    "mcp-freshness-revalidation-page-sets:v1",
                    tuple(
                        page_set.page_set_fingerprint
                        for page_set in refreshed_authority.discovery_attribution.page_set_receipts
                    ),
                ),
                request_count=request_count,
                page_count=page_count,
                observed_at_utc=_utc_now(),
            )
            with connection.dispatch_state_lock:
                other_reasons = connection.snapshot_dirty_reasons - {
                    McpSnapshotDirtyReason.TTL_EXPIRED
                }
                if other_reasons:
                    self._publish_dirty_signal(connection)
                    raise McpSnapshotReconcileRequired(
                        connection.config.server_id,
                        _dirty_reason_values(connection),
                    )
                connection.snapshot_dirty_reasons.discard(
                    McpSnapshotDirtyReason.TTL_EXPIRED
                )
                connection.freshness_generation += 1
                freshness_generation = connection.freshness_generation
                allow_expired = (
                    connection.freshness_deadline_monotonic is not None
                    and time.monotonic()
                    >= connection.freshness_deadline_monotonic
                )
            return _McpDispatchFreshnessPermit(
                receipt=receipt,
                dispatch_operation_id=dispatch_operation_id,
                freshness_generation=freshness_generation,
                allow_expired_deadline_once=allow_expired,
            )

    def _snapshot_for_server(self, server_id: str) -> McpServerSnapshot:
        matches = tuple(
            snapshot for snapshot in self._snapshots if snapshot.server_id == server_id
        )
        if len(matches) != 1 or matches[0].status is not McpServerStatus.READY:
            raise RuntimeError("MCP dispatch freshness lacks one READY snapshot")
        return matches[0]

    @staticmethod
    def _publish_dirty_signal(connection: _SdkServerConnection) -> None:
        callback = connection.dirty_callback
        if callback is not None:
            callback(_build_dirty_signal(connection))

    async def call_tool(
        self,
        binding_lease: McpManagerLease,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_ms: int,
    ) -> McpToolResult | McpClientInputRequired:
        connection = self._require_connection_for_lease(binding_lease)
        return await self._run_owned_operation(
            self._call_tool_connected(
                connection,
                binding_lease,
                tool_name,
                dict(arguments),
                timeout_ms=timeout_ms,
            ),
            name=(
                "pulsara-mcp-tool-call:"
                f"{binding_lease.binding_identity.server_id}:{tool_name}"
            ),
        )

    async def _call_tool_connected(
        self,
        connection: _SdkServerConnection,
        binding_lease: McpManagerLease,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_ms: int,
        input_responses: dict[str, dict[str, Any]] | None = None,
        request_state: str | None = None,
        round_count: int = 1,
        interaction_id: str | None = None,
        operation_deadline_monotonic: float | None = None,
    ) -> McpToolResult | McpClientInputRequired:
        semantic = self._tool_semantic(connection.config.server_id, tool_name)
        retryable_payload = build_retryable_tool_call_payload(
            tool_name=tool_name,
            arguments=arguments,
            source_method_schema_fingerprint=context_fingerprint(
                "mcp-retryable-method-schema:v1",
                {
                    "method": "tools/call",
                    "base_params": ("name", "arguments"),
                    "excluded": ("_meta", "inputResponses", "requestState"),
                },
            ),
        )
        driven = await self._drive_input_required_request(
            connection=connection,
            binding_lease=binding_lease,
            request_factory=lambda responses, state: types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name=tool_name,
                    arguments=arguments,
                    input_responses=responses,  # type: ignore[arg-type]
                    request_state=state,
                )
            ),
            result_adapter=_CALL_TOOL_RESULT_ADAPTER,
            complete_result_type=types.CallToolResult,
            retryable_payload=retryable_payload,
            timeout_ms=timeout_ms,
            input_responses=input_responses,
            request_state=request_state,
            leg_ordinal=round_count,
            interaction_id=interaction_id,
            trace_method="tools/call",
            target_kind="tool",
            target_semantic_fingerprint=semantic.tool_semantic_fingerprint,
            operation_deadline_monotonic=operation_deadline_monotonic,
        )
        if isinstance(driven, McpClientInputRequired):
            return driven
        result, operation_id = driven
        if not isinstance(result, types.CallToolResult):
            return McpToolResult(
                output=json.dumps(result.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2),
                metadata={"mcp_result_type": type(result).__name__},
            )
        raw_carrier = build_raw_tool_call_result_carrier(
            result=result,
            operation_id=operation_id,
            sdk_client_generation_id=_safe_sdk_generation_id(connection),
            tool_semantic_fingerprint=semantic.tool_semantic_fingerprint,
        )
        try:
            validate_structured_tool_result(
                tool=semantic,
                structured_content_present=raw_carrier.structured_content_present,
                structured_content=thaw_json(raw_carrier.structured_content),
            )
        except McpOutputSchemaMismatch as exc:
            return McpToolResult(
                output=f"MCP tool returned structured content that does not match outputSchema: {exc}",
                is_error=True,
                metadata={
                    "mcp_error_code": "mcp_output_schema_mismatch",
                    "mcp_operation_id": operation_id,
                    "mcp_raw_result_carrier_fingerprint": raw_carrier.carrier_fingerprint,
                    "mcp_physical_call_completed": True,
                },
            )
        return mcp_tool_result_from_sdk(
            result,
            structured_content_present=raw_carrier.structured_content_present,
        )

    async def read_resource(
        self,
        binding_lease: McpManagerLease,
        uri: str,
        *,
        timeout_ms: int,
    ) -> McpToolResult | McpClientInputRequired:
        connection = self._require_connection_for_lease(binding_lease)
        return await self._run_owned_operation(
            self._read_resource_connected(
                connection,
                binding_lease,
                uri,
                timeout_ms=timeout_ms,
            ),
            name=(
                "pulsara-mcp-resource-read:"
                f"{binding_lease.binding_identity.server_id}"
            ),
        )

    async def _read_resource_connected(
        self,
        connection: _SdkServerConnection,
        binding_lease: McpManagerLease,
        uri: str,
        *,
        timeout_ms: int,
        input_responses: dict[str, dict[str, Any]] | None = None,
        request_state: str | None = None,
        round_count: int = 1,
        interaction_id: str | None = None,
        operation_deadline_monotonic: float | None = None,
    ) -> McpToolResult | McpClientInputRequired:
        retryable_payload = build_retryable_resource_read_payload(
            uri=uri,
            source_method_schema_fingerprint=context_fingerprint(
                "mcp-retryable-method-schema:v1",
                {
                    "method": "resources/read",
                    "base_params": ("uri",),
                    "excluded": ("_meta", "inputResponses", "requestState"),
                },
            ),
        )
        driven = await self._drive_input_required_request(
            connection=connection,
            binding_lease=binding_lease,
            request_factory=lambda responses, state: types.ReadResourceRequest(
                params=types.ReadResourceRequestParams(
                    uri=uri,
                    input_responses=responses,  # type: ignore[arg-type]
                    request_state=state,
                )
            ),
            result_adapter=_READ_RESOURCE_RESULT_ADAPTER,
            complete_result_type=types.ReadResourceResult,
            retryable_payload=retryable_payload,
            timeout_ms=timeout_ms,
            input_responses=input_responses,
            request_state=request_state,
            leg_ordinal=round_count,
            interaction_id=interaction_id,
            trace_method="resources/read",
            target_kind="resource",
            target_semantic_fingerprint=self._resource_target_semantic_fingerprint(
                connection.config.server_id,
                uri,
            ),
            operation_deadline_monotonic=operation_deadline_monotonic,
        )
        if isinstance(driven, McpClientInputRequired):
            return driven
        result, _operation_id = driven
        return mcp_read_resource_result_from_sdk(result)

    async def get_prompt(
        self,
        binding_lease: McpManagerLease,
        name: str,
        arguments: dict[str, str] | None = None,
        *,
        timeout_ms: int,
    ) -> McpToolResult | McpClientInputRequired:
        connection = self._require_connection_for_lease(binding_lease)
        return await self._run_owned_operation(
            self._get_prompt_connected(
                connection,
                binding_lease,
                name,
                arguments,
                timeout_ms=timeout_ms,
            ),
            name=(
                "pulsara-mcp-prompt-get:"
                f"{binding_lease.binding_identity.server_id}:{name}"
            ),
        )

    async def _get_prompt_connected(
        self,
        connection: _SdkServerConnection,
        binding_lease: McpManagerLease,
        name: str,
        arguments: dict[str, str] | None,
        *,
        timeout_ms: int,
        input_responses: dict[str, dict[str, Any]] | None = None,
        request_state: str | None = None,
        round_count: int = 1,
        interaction_id: str | None = None,
        operation_deadline_monotonic: float | None = None,
    ) -> McpToolResult | McpClientInputRequired:
        retryable_payload = build_retryable_prompt_get_payload(
            prompt_name=name,
            arguments=arguments,
            source_method_schema_fingerprint=context_fingerprint(
                "mcp-retryable-method-schema:v1",
                {
                    "method": "prompts/get",
                    "base_params": ("name", "arguments"),
                    "excluded": ("_meta", "inputResponses", "requestState"),
                },
            ),
        )
        driven = await self._drive_input_required_request(
            connection=connection,
            binding_lease=binding_lease,
            request_factory=lambda responses, state: types.GetPromptRequest(
                params=types.GetPromptRequestParams(
                    name=name,
                    arguments=arguments,
                    input_responses=responses,  # type: ignore[arg-type]
                    request_state=state,
                )
            ),
            result_adapter=_GET_PROMPT_RESULT_ADAPTER,
            complete_result_type=types.GetPromptResult,
            retryable_payload=retryable_payload,
            timeout_ms=timeout_ms,
            input_responses=input_responses,
            request_state=request_state,
            leg_ordinal=round_count,
            interaction_id=interaction_id,
            trace_method="prompts/get",
            target_kind="prompt",
            target_semantic_fingerprint=self._prompt_semantic(
                connection.config.server_id,
                name,
            ).semantic_fingerprint,
            operation_deadline_monotonic=operation_deadline_monotonic,
        )
        if isinstance(driven, McpClientInputRequired):
            return driven
        result, _operation_id = driven
        return mcp_get_prompt_result_from_sdk(result)

    async def resume_suspended_request(
        self,
        *,
        binding_lease: McpManagerLease,
        replay_plaintext: McpReplayReadyCarrierPlaintext,
        dispatch_receipt: McpConfirmedContinuationDispatchReceipt,
        timeout_ms: int,
    ) -> McpToolResult | McpClientInputRequired:
        connection = self._require_connection_for_lease(binding_lease)
        server_id = binding_lease.binding_identity.server_id
        if dispatch_receipt.sdk_client_generation_id != _safe_sdk_generation_id(
            connection
        ):
            raise RuntimeError("MCP dispatch receipt belongs to another SDK generation")
        remaining_operation_seconds = _remaining_utc_seconds(
            dispatch_receipt.operation_expires_at_utc
        )
        if remaining_operation_seconds <= 0:
            raise TimeoutError("MCP continuation expired before physical dispatch")
        operation_deadline_monotonic = (
            time.monotonic() + remaining_operation_seconds
        )
        timeout_ms = min(
            timeout_ms,
            max(1, int(remaining_operation_seconds * 1000)),
        )
        borrow = self._wire_borrows.issue(McpSecretAccessPurpose.FRESH_WIRE_BUILD)
        try:
            base, request_state, responses = borrow.wire_retry_parts(replay_plaintext)
        finally:
            borrow.revoke()
        source = base.get("source_method")
        if source == "tools/call":
            tool_name = base.get("tool_name")
            arguments = base.get("arguments")
            if not isinstance(tool_name, str) or not isinstance(arguments, dict):
                raise RuntimeError("sealed tools/call retry payload is malformed")
            return await self._run_owned_operation(
                self._call_tool_connected(
                    connection,
                    binding_lease,
                    tool_name,
                    arguments,
                    timeout_ms=timeout_ms,
                    input_responses=responses,
                    request_state=request_state,
                    round_count=dispatch_receipt.round_ordinal + 1,
                    interaction_id=dispatch_receipt.interaction_id,
                    operation_deadline_monotonic=operation_deadline_monotonic,
                ),
                name=(
                    "pulsara-mcp-tool-resume:"
                    f"{server_id}:{dispatch_receipt.interaction_id}"
                ),
            )
        if source == "resources/read":
            uri = base.get("uri")
            if not isinstance(uri, str):
                raise RuntimeError("sealed resources/read retry payload is malformed")
            return await self._run_owned_operation(
                self._read_resource_connected(
                    connection,
                    binding_lease,
                    uri,
                    timeout_ms=timeout_ms,
                    input_responses=responses,
                    request_state=request_state,
                    round_count=dispatch_receipt.round_ordinal + 1,
                    interaction_id=dispatch_receipt.interaction_id,
                    operation_deadline_monotonic=operation_deadline_monotonic,
                ),
                name=(
                    "pulsara-mcp-resource-resume:"
                    f"{server_id}:{dispatch_receipt.interaction_id}"
                ),
            )
        if source == "prompts/get":
            prompt_name = base.get("prompt_name")
            arguments = base.get("arguments")
            if not isinstance(prompt_name, str) or (
                arguments is not None
                and not (
                    isinstance(arguments, dict)
                    and all(
                        isinstance(key, str) and isinstance(value, str)
                        for key, value in arguments.items()
                    )
                )
            ):
                raise RuntimeError("sealed prompts/get retry payload is malformed")
            return await self._run_owned_operation(
                self._get_prompt_connected(
                    connection,
                    binding_lease,
                    prompt_name,
                    arguments,
                    timeout_ms=timeout_ms,
                    input_responses=responses,
                    request_state=request_state,
                    round_count=dispatch_receipt.round_ordinal + 1,
                    interaction_id=dispatch_receipt.interaction_id,
                    operation_deadline_monotonic=operation_deadline_monotonic,
                ),
                name=(
                    "pulsara-mcp-prompt-resume:"
                    f"{server_id}:{dispatch_receipt.interaction_id}"
                ),
            )
        raise RuntimeError(f"unsupported MCP resume source method: {source}")

    def activate_subscription(self) -> None:
        """Install listening only after the Supervisor commits this slot."""

        if self._closed:
            raise RuntimeError("cannot activate subscription on a closed MCP manager")
        for connection in self._connections.values():
            self._start_subscription_owner(connection)

    def _start_subscription_owner(self, connection: _SdkServerConnection) -> None:
        if connection.subscription_task is not None:
            return
        binding = connection.protocol_binding
        if binding is None or binding.protocol_semantic.behavior_era is not (
            McpProtocolBehaviorEra.STATELESS_PER_REQUEST
        ):
            return
        capabilities = connection.client.server_capabilities
        tools_changed = bool(
            capabilities.tools is not None and capabilities.tools.list_changed
        )
        prompts_changed = bool(
            capabilities.prompts is not None and capabilities.prompts.list_changed
        )
        resources_changed = bool(
            capabilities.resources is not None
            and capabilities.resources.list_changed
        )
        if not (tools_changed or prompts_changed or resources_changed):
            return

        async def drive() -> None:
            try:
                async with connection.client.listen(
                    tools_list_changed=tools_changed,
                    prompts_list_changed=prompts_changed,
                    resources_list_changed=resources_changed,
                ) as subscription:
                    async for event in subscription:
                        _mark_snapshot_dirty(
                            connection,
                            McpSnapshotDirtyReason.LIST_CHANGED,
                            dirty_kind=_subscription_dirty_kind(event),
                        )
                        return
                _mark_snapshot_dirty(
                    connection,
                    McpSnapshotDirtyReason.TRANSPORT_RECONNECTED,
                )
            except asyncio.CancelledError:
                raise
            except BaseException:
                _mark_snapshot_dirty(
                    connection,
                    McpSnapshotDirtyReason.TRANSPORT_RECONNECTED,
                )

        connection.subscription_task = asyncio.create_task(
            drive(),
            name=f"pulsara-mcp-subscription:{connection.config.server_id}",
        )

    def cancel_active(self) -> None:
        for task in tuple(self._active_tasks):
            task.cancel()

    async def _run_owned_operation(self, awaitable, *, name: str):
        task = asyncio.create_task(awaitable, name=name)
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        return await asyncio.shield(task)

    def _tool_semantic(self, server_id: str, tool_name: str):
        for snapshot in self._snapshots:
            if snapshot.server_id != server_id:
                continue
            for tool in snapshot.tools:
                if tool.name == tool_name and tool.semantic is not None:
                    return tool.semantic
        raise RuntimeError(
            f"MCP tool semantic authority is unavailable: {server_id}/{tool_name}"
        )

    def _resource_target_semantic_fingerprint(
        self,
        server_id: str,
        uri: str,
    ) -> str:
        snapshot = self._snapshot_for_server(server_id)
        matches = tuple(
            item.semantic
            for item in snapshot.resources
            if item.uri == uri and item.semantic is not None
        )
        if len(matches) == 1:
            return matches[0].semantic_fingerprint
        return context_fingerprint(
            "mcp-resource-read-target:v1",
            {
                "server_id": server_id,
                "uri": uri,
                "snapshot_semantic_fingerprint": (
                    snapshot.snapshot_semantic_fingerprint
                ),
            },
        )

    def _prompt_semantic(self, server_id: str, prompt_name: str):
        snapshot = self._snapshot_for_server(server_id)
        matches = tuple(
            item.semantic
            for item in snapshot.prompts
            if item.name == prompt_name and item.semantic is not None
        )
        if len(matches) != 1:
            raise RuntimeError(
                "MCP prompt semantic authority is unavailable: "
                f"{server_id}/{prompt_name}"
            )
        return matches[0]

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        if self._closed:
            return
        deadline = time.monotonic() + timeout_seconds
        try:
            await asyncio.wait_for(
                self._close_lock.acquire(),
                timeout=max(0.001, deadline - time.monotonic()),
            )
        except TimeoutError as exc:
            raise McpDrainError("timed out waiting for MCP SDK close ownership") from exc
        try:
            if self._closed:
                return
            subscription_tasks = tuple(
                connection.subscription_task
                for connection in self._connections.values()
                if connection.subscription_task is not None
            )
            for task in subscription_tasks:
                task.cancel()
            if subscription_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*subscription_tasks, return_exceptions=True),
                        timeout=max(0.001, deadline - time.monotonic()),
                    )
                except TimeoutError as exc:
                    raise McpDrainError(
                        "timed out draining MCP subscription owners"
                    ) from exc
            self.cancel_active()
            if self._active_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            *tuple(self._active_tasks),
                            return_exceptions=True,
                        ),
                        timeout=max(0.001, deadline - time.monotonic()),
                    )
                except TimeoutError as exc:
                    raise McpDrainError("timed out draining active MCP SDK calls") from exc
            for server_id in tuple(self._connections):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpDrainError("MCP SDK close deadline expired")
                await self._close_connection(
                    server_id,
                    timeout_seconds=remaining,
                )
            self._closed = True
        finally:
            self._close_lock.release()

    async def _close_connection(
        self,
        server_id: str,
        *,
        timeout_seconds: float,
    ) -> None:
        connection = self._connections.get(server_id)
        if connection is None:
            return
        await _close_sdk_connection(connection, timeout_seconds=timeout_seconds)
        self._connections.pop(server_id, None)

    def _require_connection(self, server_id: str) -> _SdkServerConnection:
        if self._closed:
            raise RuntimeError("MCP SDK manager is closed")
        try:
            return self._connections[server_id]
        except KeyError as exc:
            raise KeyError(f"Unknown MCP server: {server_id}") from exc

    def _require_connection_for_lease(
        self,
        lease: McpManagerLease,
    ) -> _SdkServerConnection:
        identity = lease.binding_identity
        if lease.slot_id != identity.slot_id:
            raise RuntimeError("MCP manager lease slot identity drifted")
        connection = self._require_connection(identity.server_id)
        if (
            connection.snapshot_id != identity.snapshot_id
            or connection.discovery_generation != identity.discovery_generation
        ):
            raise RuntimeError("MCP manager lease does not match installed snapshot")
        generation = connection.client_generation
        if generation is None or not generation.accepting_operations:
            raise RuntimeError("MCP manager lease lacks an accepting client generation")
        return connection


async def discover_mcp_server(
    connection: SdkMcpConnection,
    *,
    config_epoch: int,
    reconcile_attempt_id: str,
    discovery_generation: int,
    queued_at_utc: str,
    queued_monotonic: float,
    connect_started_at_utc: str,
    connect_ended_at_utc: str,
    connect_duration_seconds: float,
    discovery_started_at_utc: str,
    discovery_started_monotonic: float,
    timeout_seconds: float,
    max_pages: int = DEFAULT_MCP_MAX_PAGES,
    max_items: int = DEFAULT_MCP_MAX_ITEMS,
) -> tuple[McpServerSnapshot, int, int]:
    """Discover one already-connected server under one absolute caller budget."""

    probe = SdkMcpClientManager(
        _snapshots=(),
        _connections={},
        max_pages=max_pages,
        max_items=max_items,
    )
    return await asyncio.wait_for(
        probe._discover_connected(
            connection._connection,
            config_epoch=config_epoch,
            reconcile_attempt_id=reconcile_attempt_id,
            discovery_generation=discovery_generation,
            queued_at_utc=queued_at_utc,
            queued_monotonic=queued_monotonic,
            connect_started_at_utc=connect_started_at_utc,
            connect_ended_at_utc=connect_ended_at_utc,
            connect_duration_seconds=connect_duration_seconds,
            discovery_started_at_utc=discovery_started_at_utc,
            discovery_started_monotonic=discovery_started_monotonic,
            install_client_generation=True,
        ),
        timeout=max(0.001, timeout_seconds),
    )


def _build_sdk_client(
    config: McpServerConfig,
    *,
    client_input_binding: McpClientInputRuntimeBinding | None = None,
) -> tuple[Client, httpx2.AsyncClient | None]:
    transport = config.transport
    elicitation_callback = (
        _reject_standalone_elicitation
        if client_input_binding is not None
        else None
    )
    if isinstance(transport, McpStdioConfig):
        env = _safe_child_env(dict(transport.env))
        params = StdioServerParameters(
            command=transport.command,
            args=list(transport.args),
            env=env,
            cwd=str(transport.cwd) if transport.cwd is not None else None,
        )
        return Client(
            stdio_client(params),
            cache=None,
            read_timeout_seconds=config.tool_timeout_ms / 1000,
            elicitation_callback=elicitation_callback,
            input_required_max_rounds=MCP_INPUT_REQUIRED_MAX_LEGS,
        ), None
    if isinstance(transport, McpStreamableHttpConfig):
        headers = _http_headers(transport)
        timeout = httpx2.Timeout(config.tool_timeout_ms / 1000)
        http_client = httpx2.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=transport.follow_redirects,
            event_hooks={"request": [inject_mcp_trace_headers_safely]},
        )
        return Client(
            streamable_http_client(transport.url, http_client=http_client),
            cache=None,
            read_timeout_seconds=config.tool_timeout_ms / 1000,
            elicitation_callback=elicitation_callback,
            input_required_max_rounds=MCP_INPUT_REQUIRED_MAX_LEGS,
        ), http_client
    raise TypeError(f"unsupported MCP transport: {type(transport).__name__}")


async def _install_stable_negotiation_authority(
    connection: _SdkServerConnection,
) -> None:
    endpoint = _build_endpoint_attribution(connection.config)
    auth = _build_auth_attribution(
        connection.config,
        connection.client_input_binding,
    )
    client_input_binding = connection.client_input_binding
    capability_policy = build_mcp_protocol_fact(
        McpClientCapabilityPolicyFact,
        schema_version="mcp_client_capability_policy.v1",
        supported_input_methods=(
            (McpClientInputMethod.ELICITATION_CREATE,)
            if client_input_binding is not None
            else ()
        ),
        elicitation_modes=(
            (McpElicitationMode.FORM, McpElicitationMode.URL)
            if client_input_binding is not None
            else ()
        ),
        elicitation_host_contract_fingerprint=(
            client_input_binding.host_contract_fingerprint
            if client_input_binding is not None
            else None
        ),
        sampling_advertised=False,
        roots_advertised=False,
        logging_advertised=False,
        ordered_extension_ads=(),
    )
    revision = connection.client.protocol_version
    if behavior_era_for_protocol_revision(revision) is (
        McpProtocolBehaviorEra.STATELESS_PER_REQUEST
    ):
        operation_id = f"mcp_final_discover:{uuid4().hex}"
        raw_result = await connection.client.session.send_discover(revision)
        discover_result = types.DiscoverResult.model_validate(raw_result)
        if revision not in discover_result.supported_versions:
            raise RuntimeError("final MCP discover dropped negotiated protocol revision")
        connection.client.session.adopt(discover_result)
        receipt = build_mcp_protocol_fact(
            McpFinalDiscoverWireReceiptFact,
            schema_version="mcp_final_discover_wire_receipt.v1",
            physical_operation_id=operation_id,
            sdk_client_generation_id=connection.sdk_client_generation_id,
            exact_protocol_revision=revision,
            client_capability_policy_fingerprint=capability_policy.policy_fingerprint,
            endpoint_attribution_fingerprint=endpoint.attribution_fingerprint,
            auth_attribution_fingerprint=auth.attribution_fingerprint,
            raw_result_payload_fingerprint=context_fingerprint(
                "mcp-final-discover-raw-result:v1",
                raw_result,
            ),
        )
        negotiation_source = "server_discover"
    else:
        initialize_result = connection.client.session.initialize_result
        if initialize_result is None:
            raise RuntimeError("legacy MCP connection lacks initialize receipt")
        raw_result = initialize_result.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        receipt = build_mcp_protocol_fact(
            McpLegacyInitializeWireReceiptFact,
            schema_version="mcp_legacy_initialize_wire_receipt.v1",
            physical_operation_id=f"mcp_initialize:{uuid4().hex}",
            sdk_client_generation_id=connection.sdk_client_generation_id,
            exact_protocol_revision=revision,
            client_capability_policy_fingerprint=capability_policy.policy_fingerprint,
            endpoint_attribution_fingerprint=endpoint.attribution_fingerprint,
            auth_attribution_fingerprint=auth.attribution_fingerprint,
            raw_result_payload_fingerprint=context_fingerprint(
                "mcp-legacy-initialize-raw-result:v1",
                raw_result,
            ),
        )
        negotiation_source = "legacy_initialize"
    capabilities_payload = connection.client.server_capabilities.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )
    protocol_semantic = build_mcp_protocol_fact(
        McpServerProtocolSemanticFact,
        schema_version="mcp_server_protocol_semantic.v1",
        protocol_revision=revision,
        behavior_era=behavior_era_for_protocol_revision(revision),
        server_capabilities=_freeze_json_object(capabilities_payload),
        ordered_extension_contracts=(),
    )
    server_info = connection.client.server_info
    server_info_fact = (
        _freeze_json_object(
            server_info.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
        if server_info is not None
        else None
    )
    negotiation = build_mcp_protocol_fact(
        McpProtocolNegotiationAttributionFact,
        schema_version="mcp_protocol_negotiation_attribution.v1",
        protocol_semantic_fingerprint=protocol_semantic.semantic_fingerprint,
        client_capability_policy_fingerprint=capability_policy.policy_fingerprint,
        negotiation_source=negotiation_source,
        negotiation_wire_receipt_fingerprint=receipt.receipt_fingerprint,
        sdk_version="2.0.0",
        sdk_conformance_contract_fingerprint=MCP_SDK_CONFORMANCE_CONTRACT_FINGERPRINT,
        server_info=server_info_fact,
        endpoint_attribution_fingerprint=endpoint.attribution_fingerprint,
        auth_attribution_fingerprint=auth.attribution_fingerprint,
    )
    binding = McpSdkNegotiatedProtocolBinding(
        sdk_client_generation_id=connection.sdk_client_generation_id,
        transport_generation=1,
        client=connection.client,
        negotiation_wire_receipt=receipt,
        protocol_semantic=protocol_semantic,
        client_capability_policy_fingerprint=capability_policy.policy_fingerprint,
    )
    connection.endpoint_attribution = endpoint
    connection.auth_attribution = auth
    connection.capability_policy = capability_policy
    connection.protocol_binding = binding
    connection.negotiation_attribution = negotiation


async def _reject_standalone_elicitation(
    _context: object,
    _params: object,
) -> types.ElicitResult | types.ErrorData:
    """Advertise both v2 modes while rejecting legacy standalone callbacks.

    Pulsara consumes 2026-07-28 elicitation only as an InputRequiredResult from
    its raw request seam.  Reaching this callback means the server used a
    different ownership path and must fail closed.
    """

    return types.ErrorData(
        code=-32601,
        message="Standalone elicitation is not owned by Pulsara's MRTR driver",
    )


def _disabled_mcp_secret_commitment(_domain: str, _payload: bytes) -> str:
    raise RuntimeError("MCP continuation secret binding is disabled")


def _build_endpoint_attribution(config: McpServerConfig) -> McpEndpointAttributionFact:
    transport = config.transport
    if isinstance(transport, McpStdioConfig):
        target = {
            "command": transport.command,
            "args": transport.args,
            "cwd": str(transport.cwd) if transport.cwd is not None else None,
        }
        return build_mcp_protocol_fact(
            McpEndpointAttributionFact,
            schema_version="mcp_endpoint_attribution.v1",
            transport_kind="stdio",
            canonical_target_fingerprint=context_fingerprint(
                "mcp-stdio-target:v1",
                target,
            ),
            tls_policy_fingerprint=None,
            redirect_policy="deny",
            executable_identity_fingerprint=context_fingerprint(
                "mcp-stdio-executable:v1",
                {"command": transport.command},
            ),
        )
    split = urlsplit(transport.url)
    target = urlunsplit(
        (
            split.scheme.lower(),
            (split.hostname or "").lower()
            + (f":{split.port}" if split.port is not None else ""),
            split.path or "/",
            "",
            "",
        )
    )
    return build_mcp_protocol_fact(
        McpEndpointAttributionFact,
        schema_version="mcp_endpoint_attribution.v1",
        transport_kind="streamable_http",
        canonical_target_fingerprint=context_fingerprint(
            "mcp-http-target:v1",
            target,
        ),
        tls_policy_fingerprint=context_fingerprint(
            "mcp-http-tls-policy:v1",
            {"scheme": split.scheme.lower(), "verify": True},
        ),
        redirect_policy="same_origin" if transport.follow_redirects else "deny",
        executable_identity_fingerprint=None,
    )


def _build_auth_attribution(
    config: McpServerConfig,
    client_input_binding: McpClientInputRuntimeBinding | None,
) -> McpAuthAttributionFact:
    transport = config.transport
    if isinstance(transport, McpStdioConfig):
        return build_mcp_protocol_fact(
            McpAuthAttributionFact,
            schema_version="mcp_auth_attribution.v1",
            auth_kind="none",
            issuer_identity_fingerprint=None,
            client_identity_fingerprint=None,
            effective_scope_fingerprint=None,
            credential_generation=0,
            keyed_credential_commitment=None,
        )
    secret_values: list[str] = []
    if transport.bearer_token_env_var:
        token = os.getenv(transport.bearer_token_env_var)
        if token:
            secret_values.append(token)
        auth_kind = "bearer_env"
    elif transport.headers or transport.env_headers:
        auth_kind = "static_headers"
    else:
        auth_kind = "none"
    for header, env_name in transport.env_headers.items():
        value = os.getenv(env_name)
        if value:
            secret_values.append(f"{header}:{value}")
    for header, value in transport.headers.items():
        if header.casefold() in {"authorization", "x-api-key"}:
            secret_values.append(f"{header}:{value}")
    commitment = None
    if secret_values:
        joined = "\0".join(secret_values)
        commitment = (
            client_input_binding.keyed_commitment(
                "mcp-auth-attribution:v1",
                joined.encode("utf-8"),
            )
            if client_input_binding is not None
            else runtime_mcp_secret_commitment(
                "mcp-auth-attribution:v1",
                joined,
            )
        )
    return build_mcp_protocol_fact(
        McpAuthAttributionFact,
        schema_version="mcp_auth_attribution.v1",
        auth_kind=auth_kind,
        issuer_identity_fingerprint=context_fingerprint(
            "mcp-auth-issuer:v1",
            {
                "bearer_env": transport.bearer_token_env_var,
                "env_header_names": tuple(sorted(transport.env_headers)),
            },
        )
        if auth_kind != "none"
        else None,
        client_identity_fingerprint=None,
        effective_scope_fingerprint=context_fingerprint(
            "mcp-auth-scope:v1",
            tuple(sorted(key.casefold() for key in (*transport.headers, *transport.env_headers))),
        )
        if auth_kind != "none"
        else None,
        credential_generation=1 if auth_kind != "none" else 0,
        keyed_credential_commitment=commitment,
    )


def _freeze_json_object(value: object) -> FrozenJsonObjectFact:
    frozen = freeze_json(value)
    if not isinstance(frozen, FrozenJsonObjectFact):
        raise TypeError("MCP authority requires a JSON object")
    return frozen


def _build_snapshot_authority(
    *,
    connection: _SdkServerConnection,
    snapshot_id: str,
    config_epoch: int,
    discovery_generation: int,
    reconcile_attempt_id: str,
    captures: dict[str, _SdkListingCapture],
    tools: tuple[McpDiscoveredTool, ...],
    resources: tuple[McpDiscoveredResource, ...],
    resource_templates: tuple[McpDiscoveredResourceTemplate, ...],
    prompts: tuple[McpDiscoveredPrompt, ...],
    instructions: str | None,
    tool_rejections: tuple[McpToolDiscoveryRejectionFact, ...],
) -> McpServerSnapshotAuthorityFact | None:
    binding = connection.protocol_binding
    endpoint = connection.endpoint_attribution
    auth = connection.auth_attribution
    negotiation = connection.negotiation_attribution
    if binding is None or endpoint is None or auth is None or negotiation is None:
        return None
    tool_semantics = tuple(
        sorted(
            (item.semantic for item in tools if item.semantic is not None),
            key=lambda item: item.name,
        )
    )
    resource_semantics = tuple(
        sorted(
            (item.semantic for item in resources if item.semantic is not None),
            key=lambda item: item.uri,
        )
    )
    template_semantics = tuple(
        sorted(
            (item.semantic for item in resource_templates if item.semantic is not None),
            key=lambda item: item.uri_template,
        )
    )
    prompt_semantics = tuple(
        sorted(
            (item.semantic for item in prompts if item.semantic is not None),
            key=lambda item: item.name,
        )
    )
    if (
        len(tool_semantics) != len(tools)
        or len(resource_semantics) != len(resources)
        or len(template_semantics) != len(resource_templates)
        or len(prompt_semantics) != len(prompts)
    ):
        raise RuntimeError("MCP snapshot semantic coverage is incomplete")
    surface = build_mcp_protocol_fact(
        McpServerSurfaceSemanticFact,
        schema_version="mcp_server_surface_semantic.v1",
        server_id=connection.config.server_id,
        protocol_semantic=binding.protocol_semantic,
        tools=tool_semantics,
        resources=resource_semantics,
        resource_templates=template_semantics,
        prompts=prompt_semantics,
        instructions=instructions,
    )
    page_sets = tuple(
        sorted(
            (capture.page_set for capture in captures.values()),
            key=lambda item: item.method.value,
        )
    )
    discovery = build_mcp_protocol_fact(
        McpDiscoveryAttributionFact,
        schema_version="mcp_discovery_attribution.v1",
        snapshot_id=snapshot_id,
        config_epoch=config_epoch,
        discovery_generation=discovery_generation,
        transport_generation=binding.transport_generation,
        endpoint=endpoint,
        auth=auth,
        negotiation=negotiation,
        page_set_receipts=page_sets,
        ordered_tool_rejections=tool_rejections,
        reconcile_attempt_id=reconcile_attempt_id,
    )
    projections = tuple(
        sorted(
            (
                item.provider_projection
                for item in tools
                if item.provider_projection is not None
            ),
            key=lambda item: item.tool_semantic_fingerprint,
        )
    )
    if len(projections) != len(tools):
        raise RuntimeError("MCP provider projection coverage is incomplete")
    return build_mcp_protocol_fact(
        McpServerSnapshotAuthorityFact,
        schema_version="mcp_server_snapshot_authority.v1",
        surface_semantic=surface,
        discovery_attribution=discovery,
        ordered_provider_projections=projections,
        surface_semantic_fingerprint=surface.surface_semantic_fingerprint,
        projection_accumulator=context_fingerprint(
            "mcp-provider-projection-accumulator:v1",
            tuple(item.projection_fingerprint for item in projections),
        ),
    )


def _install_conformed_client_generation(
    *,
    connection: _SdkServerConnection,
    snapshot: McpServerSnapshot,
    tool_capture: _SdkListingCapture | None,
    all_tool_facts: tuple[McpDiscoveredTool, ...],
    tool_rejections: tuple[McpToolDiscoveryRejectionFact, ...],
) -> None:
    binding = connection.protocol_binding
    authority = snapshot.authority
    if binding is None or authority is None:
        raise RuntimeError("MCP client generation requires complete authority")
    tool_attributions = tuple(
        sorted(
            item.discovery_attribution.attribution_fingerprint
            for item in all_tool_facts
            if item.discovery_attribution is not None
        )
    )
    if len(tool_attributions) != len(all_tool_facts):
        raise RuntimeError("MCP client generation lacks tool attribution")
    listing_accumulator = context_fingerprint(
        "mcp-sdk-complete-tool-listing:v1",
        {
            "sdk_client_generation_id": connection.sdk_client_generation_id,
            "page_set_fingerprint": (
                tool_capture.page_set.page_set_fingerprint
                if tool_capture is not None
                else None
            ),
            "ordered_tool_attribution_fingerprints": tool_attributions,
            "ordered_tool_rejection_fingerprints": tuple(
                sorted(item.rejection_fingerprint for item in tool_rejections)
            ),
        },
    )
    final_binding = McpSdkProtocolBinding(
        sdk_client_generation_id=binding.sdk_client_generation_id,
        transport_generation=binding.transport_generation,
        client=binding.client,
        negotiation_wire_receipt=binding.negotiation_wire_receipt,
        protocol_semantic=binding.protocol_semantic,
        client_capability_policy_fingerprint=(
            binding.client_capability_policy_fingerprint
        ),
        complete_listing_accumulator=listing_accumulator,
    )
    generation = McpSdkConformedClientGeneration(
        generation_id=connection.sdk_client_generation_id,
        sdk_protocol_binding=final_binding,
        final_negotiation_wire_receipt=final_binding.negotiation_wire_receipt,
        client=connection.client,
        snapshot_id=snapshot.snapshot_id,
        snapshot_semantic_fingerprint=snapshot.snapshot_semantic_fingerprint,
        snapshot_authority_fingerprint=authority.authority_fingerprint,
        complete_tool_listing_accumulator=listing_accumulator,
        ordered_tool_attribution_fingerprints=tool_attributions,
        accepting_operations=True,
    )
    if (
        authority.discovery_attribution.negotiation.negotiation_wire_receipt_fingerprint
        != generation.final_negotiation_wire_receipt.receipt_fingerprint
    ):
        raise RuntimeError("MCP client generation negotiation authority drifted")
    connection.protocol_binding = final_binding
    connection.client_generation = generation


def _sdk_wire_field_present(
    result: object,
    *,
    field_name: str,
    alias: str,
) -> bool:
    fields_set = getattr(result, "model_fields_set", None)
    if fields_set is not None:
        return field_name in fields_set or alias in fields_set
    return hasattr(result, field_name)


def _sdk_result_payload(result: object) -> object:
    dump = getattr(result, "model_dump", None)
    if callable(dump):
        return dump(mode="json", by_alias=True, exclude_unset=True)
    values = vars(result) if hasattr(result, "__dict__") else {"value": str(result)}
    return _json_safe_sdk_value(values)


def _json_safe_sdk_value(value: object) -> object:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json", by_alias=True, exclude_unset=True)
    if isinstance(value, dict):
        return {str(key): _json_safe_sdk_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_sdk_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


async def _close_sdk_connection(
    connection: _SdkServerConnection,
    *,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    if connection.close_requested is not None:
        connection.close_requested.set()
    if connection.owner_task is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise McpDrainError("MCP SDK owner close deadline expired")
        try:
            await asyncio.wait_for(
                asyncio.shield(connection.owner_task),
                timeout=remaining,
            )
        except TimeoutError as exc:
            raise McpDrainError("timed out draining MCP SDK owner task") from exc
        except asyncio.CancelledError:
            if not connection.owner_task.cancelled():
                raise
    if connection.http_client is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise McpDrainError("MCP HTTP client close deadline expired")
        try:
            await asyncio.wait_for(connection.http_client.aclose(), timeout=remaining)
        except TimeoutError as exc:
            raise McpDrainError("timed out closing MCP HTTP client") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _remaining_utc_seconds(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("MCP continuation expiry must be timezone-aware")
    return (parsed.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()


async def _start_sdk_client_owner(
    client: Client,
    *,
    timeout_seconds: float,
) -> tuple[asyncio.Event, asyncio.Task[None]]:
    """Enter and exit the SDK client context from one dedicated task.

    MCP SDK v2 streamable-http transports use anyio cancel scopes that must
    be exited by the same task that entered them.  Keeping a tiny owner task
    alive for the lifetime of the connection prevents close-time cancellation
    from leaking into HostCore/REPL teardown.
    """

    ready = asyncio.Event()
    close_requested = asyncio.Event()
    enter_error: BaseException | None = None

    async def owner() -> None:
        nonlocal enter_error
        try:
            await client.__aenter__()
        except BaseException as exc:
            enter_error = exc
            ready.set()
            return
        ready.set()
        try:
            await close_requested.wait()
        finally:
            await _best_effort_sdk_close_step(client.__aexit__(None, None, None))

    task = asyncio.create_task(owner(), name="pulsara-mcp-sdk-client-owner")
    try:
        await asyncio.wait_for(ready.wait(), timeout=timeout_seconds)
    except BaseException as exc:
        close_requested.set()
        task.cancel()
        raise _SdkOwnerStartError(
            exc,
            close_requested=close_requested,
            owner_task=task,
        ) from exc
    if enter_error is not None:
        with contextlib.suppress(BaseException):
            await task
        raise enter_error
    return close_requested, task


async def _best_effort_sdk_close_step(awaitable: Any) -> None:
    """Run one SDK close step without leaking internal cancel-scope shutdown.

    MCP SDK v2 transports may use cancellation as part of normal ``__aexit__``
    teardown.  Pulsara owns the host/session lifecycle above this facade, so an
    SDK-internal close cancellation must not poison the REPL task or make
    ``:close`` fail after the server has already been detached.
    """

    try:
        await awaitable
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        _clear_current_task_cancellation()
    except Exception:
        pass


@contextlib.asynccontextmanager
async def _mcp_operation_lane(
    connection: _SdkServerConnection,
    *,
    binding_lease: McpManagerLease,
    operation_id: str,
    target_kind: str,
    target_semantic_fingerprint: str,
    freshness_permit: _McpDispatchFreshnessPermit | None = None,
):
    dirty_signal: McpServerDirtySignal | None = None
    dispatch_borrow: McpBindingDispatchBorrow | None = None
    concurrency_mode: McpSdkConcurrencyMode | None = None
    freshness_receipt_fingerprint: str | None = None
    with connection.dispatch_state_lock:
        generation = connection.client_generation
        permit_is_current = False
        if freshness_permit is not None:
            if (
                generation is None
                or connection.snapshot_id is None
                or connection.snapshot_semantic_fingerprint is None
            ):
                raise RuntimeError("MCP freshness permit lacks installed authority")
            freshness_permit.require_current(
                server_id=connection.config.server_id,
                snapshot_id=connection.snapshot_id,
                snapshot_semantic_fingerprint=(
                    connection.snapshot_semantic_fingerprint
                ),
                snapshot_authority_fingerprint=(
                    generation.snapshot_authority_fingerprint
                ),
                sdk_client_generation_id=generation.generation_id,
                freshness_generation=connection.freshness_generation,
                operation_id=operation_id,
            )
            permit_is_current = True
            freshness_receipt_fingerprint = (
                freshness_permit.receipt.receipt_fingerprint
            )
        deadline = connection.freshness_deadline_monotonic
        if (
            deadline is not None
            and time.monotonic() >= deadline
            and not (
                permit_is_current
                and freshness_permit is not None
                and freshness_permit.allow_expired_deadline_once
            )
        ):
            was_clean = not connection.snapshot_dirty_reasons
            connection.snapshot_dirty_reasons.add(
                McpSnapshotDirtyReason.TTL_EXPIRED
            )
            if was_clean:
                dirty_signal = _build_dirty_signal(connection)
        dirty_reasons = _dirty_reason_values(connection)
        if not dirty_reasons:
            revision = _safe_protocol_version(connection.client)
            if revision is None:
                raise RuntimeError(
                    "MCP operation cannot dispatch before protocol negotiation"
                )
            identity = binding_lease.binding_identity
            generation = connection.client_generation
            protocol = connection.protocol_binding
            endpoint = connection.endpoint_attribution
            auth = connection.auth_attribution
            if (
                binding_lease.slot_id != identity.slot_id
                or identity.server_id != connection.config.server_id
                or identity.snapshot_id != connection.snapshot_id
                or identity.discovery_generation != connection.discovery_generation
                or connection.snapshot_semantic_fingerprint is None
                or connection.config_epoch is None
                or connection.transport_generation is None
                or generation is None
                or not generation.accepting_operations
                or generation.client is not connection.client
                or generation.snapshot_id != connection.snapshot_id
                or generation.snapshot_semantic_fingerprint
                != connection.snapshot_semantic_fingerprint
                or protocol is None
                or generation.sdk_protocol_binding is not protocol
                or endpoint is None
                or auth is None
                or protocol.protocol_semantic.protocol_revision != revision
            ):
                raise RuntimeError(
                    "MCP dispatch borrow cannot join installed binding authority"
                )
            borrow_freshness_generation = (
                freshness_permit.freshness_generation
                if freshness_permit is not None
                else connection.freshness_generation
            )
            dispatch_borrow = build_mcp_binding_dispatch_borrow(
                operation_id=operation_id,
                binding_lease_id=binding_lease.lease_id,
                slot_id=identity.slot_id,
                server_id=identity.server_id,
                snapshot_id=identity.snapshot_id,
                snapshot_semantic_fingerprint=(
                    connection.snapshot_semantic_fingerprint
                ),
                snapshot_authority_fingerprint=(
                    generation.snapshot_authority_fingerprint
                ),
                config_epoch=connection.config_epoch,
                discovery_generation=identity.discovery_generation,
                sdk_client_generation_id=generation.generation_id,
                transport_generation=connection.transport_generation,
                protocol_semantic_fingerprint=(
                    protocol.protocol_semantic.semantic_fingerprint
                ),
                endpoint_attribution_fingerprint=endpoint.attribution_fingerprint,
                auth_attribution_fingerprint=auth.attribution_fingerprint,
                target_kind=target_kind,
                target_semantic_fingerprint=target_semantic_fingerprint,
                dirty_signal_generation=connection.dirty_signal_generation,
                freshness_generation=borrow_freshness_generation,
                freshness_revalidation_receipt_fingerprint=(
                    freshness_receipt_fingerprint
                ),
            )
            if freshness_permit is not None:
                freshness_permit.consume()
            connection.admitted_operation_count += 1
            concurrency_mode = _mcp_sdk_concurrency_mode(connection)
        else:
            concurrency_mode = None
    if dirty_signal is not None and connection.dirty_callback is not None:
        connection.dirty_callback(dirty_signal)
    if dirty_reasons:
        raise McpSnapshotReconcileRequired(
            connection.config.server_id,
            dirty_reasons,
        )
    try:
        if dispatch_borrow is None:
            raise RuntimeError("MCP operation lacks a dispatch borrow")
        if concurrency_mode is McpSdkConcurrencyMode.BOUNDED_PARALLEL:
            async with connection.stateless_semaphore:
                yield dispatch_borrow
            return
        if concurrency_mode is not McpSdkConcurrencyMode.SERIALIZED:
            raise RuntimeError("MCP operation lacks a concurrency mode")
        async with connection.lock:
            yield dispatch_borrow
    finally:
        if dispatch_borrow is not None and dispatch_borrow.active:
            dispatch_borrow.release()
        with connection.dispatch_state_lock:
            if dispatch_borrow is not None:
                connection.admitted_operation_count -= 1
                if connection.admitted_operation_count < 0:
                    raise RuntimeError("MCP admitted operation accounting underflow")


def _mark_snapshot_dirty(
    connection: _SdkServerConnection,
    reason: McpSnapshotDirtyReason,
    *,
    dirty_kind: McpSubscriptionDirtyKind | None = None,
) -> None:
    with connection.dispatch_state_lock:
        was_clean = not connection.snapshot_dirty_reasons
        connection.snapshot_dirty_reasons.add(reason)
        if dirty_kind is not None:
            connection.subscription_dirty_kinds.add(dirty_kind)
        signal = _build_dirty_signal(connection) if was_clean else None
    if signal is not None and connection.dirty_callback is not None:
        connection.dirty_callback(signal)


def _subscription_dirty_kind(event: object) -> McpSubscriptionDirtyKind | None:
    name = type(event).__name__.casefold()
    if "tool" in name:
        return "tools"
    if "prompt" in name:
        return "prompts"
    if "resource" in name:
        return "resources"
    return None


def _dirty_reason_values(connection: _SdkServerConnection) -> tuple[str, ...]:
    return tuple(sorted(reason.value for reason in connection.snapshot_dirty_reasons))


def _build_dirty_signal(connection: _SdkServerConnection) -> McpServerDirtySignal:
    if (
        connection.snapshot_id is None
        or connection.config_epoch is None
        or connection.discovery_generation is None
        or connection.transport_generation is None
    ):
        raise RuntimeError("MCP dirty signal lacks installed snapshot authority")
    connection.dirty_signal_generation += 1
    return McpServerDirtySignal(
        server_id=connection.config.server_id,
        snapshot_id=connection.snapshot_id,
        config_epoch=connection.config_epoch,
        discovery_generation=connection.discovery_generation,
        transport_generation=connection.transport_generation,
        signal_generation=connection.dirty_signal_generation,
        dirty_reasons=tuple(
            sorted(connection.snapshot_dirty_reasons, key=lambda item: item.value)
        ),
        dirty_kinds=tuple(sorted(connection.subscription_dirty_kinds)),
        observed_monotonic=time.monotonic(),
    )


def _clear_current_task_cancellation() -> None:
    task = asyncio.current_task()
    if task is None or not hasattr(task, "uncancel"):
        return
    while task.cancelling():
        task.uncancel()


def _safe_child_env(explicit_env: dict[str, str]) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _SAFE_AMBIENT_ENV}
    env.update({str(key): str(value) for key, value in explicit_env.items()})
    return env


def _http_headers(transport: McpStreamableHttpConfig) -> dict[str, str]:
    headers = dict(transport.headers)
    for header, env_var in transport.env_headers.items():
        value = os.getenv(env_var)
        if value:
            headers[header] = value
    if transport.bearer_token_env_var:
        token = os.getenv(transport.bearer_token_env_var)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            raise RuntimeError(f"missing bearer token env var {transport.bearer_token_env_var}")
    return headers


def _tool_from_sdk(
    server_id: str,
    tool: types.Tool,
    *,
    source_page_receipt_fingerprint: str,
    listing_generation_fingerprint: str,
) -> McpDiscoveredTool:
    annotations = tool.annotations
    conformed = build_conformed_tool_schema(
        server_id=server_id,
        name=tool.name,
        title=tool.title,
        description=tool.description,
        input_schema=tool.input_schema,
        output_schema=tool.output_schema,
        annotations=(
            annotations.model_dump(mode="json", by_alias=True, exclude_none=True)
            if annotations is not None
            else {}
        ),
        icons=tuple(
            item.model_dump(mode="json", by_alias=True, exclude_none=True)
            for item in (tool.icons or ())
        ),
        execution=(
            tool.execution.model_dump(mode="json", by_alias=True, exclude_none=True)
            if tool.execution is not None
            else None
        ),
        protocol_meta=tool.meta,
    )
    attribution = build_mcp_protocol_fact(
        McpToolDiscoveryAttributionFact,
        schema_version="mcp_tool_discovery_attribution.v1",
        tool_semantic_fingerprint=conformed.semantic.tool_semantic_fingerprint,
        source_page_receipt_fingerprint=source_page_receipt_fingerprint,
        sdk_conformance_contract_fingerprint=MCP_SDK_CONFORMANCE_CONTRACT_FINGERPRINT,
        sdk_conformed_listing_generation_fingerprint=listing_generation_fingerprint,
        sdk_header_routing_contract_fingerprint=MCP_SDK_CONFORMANCE_CONTRACT_FINGERPRINT,
        pulsara_output_validation_contract_fingerprint=MCP_OUTPUT_VALIDATION_CONTRACT_FINGERPRINT,
    )
    return McpDiscoveredTool(
        server_id=server_id,
        name=tool.name,
        description=tool.description or tool.name,
        input_schema=thaw_json(conformed.semantic.input_schema),
        annotations=McpToolAnnotations(
            read_only_hint=getattr(annotations, "read_only_hint", None) if annotations is not None else None,
            destructive_hint=getattr(annotations, "destructive_hint", None) if annotations is not None else None,
            open_world_hint=getattr(annotations, "open_world_hint", None) if annotations is not None else None,
            title=getattr(annotations, "title", None) if annotations is not None else None,
        ),
        title=tool.title,
        output_schema=(
            thaw_json(conformed.semantic.output_schema)
            if conformed.semantic.output_schema is not None
            else None
        ),
        semantic=conformed.semantic,
        discovery_attribution=attribution,
        provider_projection=conformed.provider_projection,
    )


def _resource_from_sdk(server_id: str, resource: types.Resource) -> McpDiscoveredResource:
    semantic = build_mcp_protocol_fact(
        McpResourceSemanticFact,
        schema_version="mcp_resource_semantic.v1",
        server_id=server_id,
        uri=str(resource.uri),
        name=resource.name,
        title=resource.title,
        description=resource.description,
        mime_type=resource.mime_type,
        size=resource.size,
        annotations=_freeze_json_object(
            resource.annotations.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
            if resource.annotations is not None
            else {}
        ),
        icons=tuple(
            _freeze_json_object(
                icon.model_dump(mode="json", by_alias=True, exclude_none=True)
            )
            for icon in (resource.icons or ())
        ),
        protocol_meta=(
            _freeze_json_object(resource.meta) if resource.meta is not None else None
        ),
    )
    return McpDiscoveredResource(
        server_id=server_id,
        uri=str(resource.uri),
        name=resource.name,
        description=resource.description or "",
        mime_type=resource.mime_type,
        size=resource.size,
        semantic=semantic,
    )


def _resource_template_from_sdk(
    server_id: str,
    template: types.ResourceTemplate,
) -> McpDiscoveredResourceTemplate:
    semantic = build_mcp_protocol_fact(
        McpResourceTemplateSemanticFact,
        schema_version="mcp_resource_template_semantic.v1",
        server_id=server_id,
        uri_template=template.uri_template,
        name=template.name,
        title=template.title,
        description=template.description,
        mime_type=template.mime_type,
        annotations=_freeze_json_object(
            template.annotations.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
            if template.annotations is not None
            else {}
        ),
        icons=tuple(
            _freeze_json_object(
                icon.model_dump(mode="json", by_alias=True, exclude_none=True)
            )
            for icon in (template.icons or ())
        ),
        protocol_meta=(
            _freeze_json_object(template.meta) if template.meta is not None else None
        ),
    )
    return McpDiscoveredResourceTemplate(
        server_id=server_id,
        uri_template=template.uri_template,
        name=template.name,
        description=template.description or "",
        mime_type=template.mime_type,
        semantic=semantic,
    )


def _prompt_from_sdk(server_id: str, prompt: types.Prompt) -> McpDiscoveredPrompt:
    arguments = tuple(
        build_mcp_protocol_fact(
            McpPromptArgumentSemanticFact,
            schema_version="mcp_prompt_argument_semantic.v1",
            name=argument.name,
            title=argument.title,
            description=argument.description,
            required=bool(argument.required),
        )
        for argument in (prompt.arguments or ())
    )
    semantic = build_mcp_protocol_fact(
        McpPromptSemanticFact,
        schema_version="mcp_prompt_semantic.v1",
        server_id=server_id,
        name=prompt.name,
        title=prompt.title,
        description=prompt.description,
        arguments=arguments,
        icons=tuple(
            _freeze_json_object(
                icon.model_dump(mode="json", by_alias=True, exclude_none=True)
            )
            for icon in (prompt.icons or ())
        ),
        protocol_meta=(
            _freeze_json_object(prompt.meta) if prompt.meta is not None else None
        ),
    )
    return McpDiscoveredPrompt(
        server_id=server_id,
        name=prompt.name,
        description=prompt.description or "",
        arguments=tuple(
            argument.model_dump(mode="json", by_alias=True, exclude_none=True)
            for argument in (prompt.arguments or ())
        ),
        semantic=semantic,
    )


def mcp_tool_result_from_sdk(
    result: types.CallToolResult,
    *,
    structured_content_present: bool | None = None,
) -> McpToolResult:
    output_parts: list[str] = []
    artifacts: list[McpContentArtifact] = []
    for index, item in enumerate(result.content):
        _append_content(item, output_parts, artifacts, role_prefix=f"content_{index}")
    if structured_content_present is None:
        structured_content_present = "structured_content" in result.model_fields_set
    if structured_content_present:
        structured_text = json.dumps(result.structured_content, ensure_ascii=False, indent=2, sort_keys=True)
        output_parts.append(f"[structured_content]\n{structured_text}")
        artifacts.append(
            McpContentArtifact(
                role="structured_content",
                media_type="application/json",
                text=structured_text,
                metadata={"mcp_content_kind": "structured_content"},
            )
        )
    output = "\n\n".join(part for part in output_parts if part).strip()
    if not output:
        output = "[MCP tool returned non-text content; see artifacts/metadata.]"
    return McpToolResult(
        output=output,
        is_error=result.is_error,
        structured_content=result.structured_content,
        artifacts=tuple(artifacts),
        metadata={
            "mcp_result_type": "CallToolResult",
            "mcp_is_error": result.is_error,
            "mcp_content_count": len(result.content),
            "mcp_structured_content_present": structured_content_present,
        },
    )


def mcp_read_resource_result_from_sdk(result: types.ReadResourceResult) -> McpToolResult:
    output_parts: list[str] = []
    artifacts: list[McpContentArtifact] = []
    for index, content in enumerate(result.contents):
        if isinstance(content, types.TextResourceContents):
            output_parts.append(f"[resource:{content.uri}]\n{content.text}")
            artifacts.append(
                McpContentArtifact(
                    role=f"resource_{index}",
                    media_type=content.mime_type or "text/plain; charset=utf-8",
                    text=content.text,
                    metadata={"uri": content.uri, "mcp_content_kind": "text_resource"},
                )
            )
        elif isinstance(content, types.BlobResourceContents):
            data = _decode_base64(content.blob)
            artifacts.append(
                McpContentArtifact(
                    role=f"resource_{index}",
                    media_type=content.mime_type or "application/octet-stream",
                    data=data,
                    metadata={"uri": content.uri, "mcp_content_kind": "blob_resource"},
                )
            )
            output_parts.append(f"[resource_blob:{content.uri}] {len(data)} bytes archived")
    return McpToolResult(
        output="\n\n".join(output_parts).strip() or "[MCP resource contained no model-visible text.]",
        artifacts=tuple(artifacts),
        metadata={"mcp_result_type": "ReadResourceResult", "mcp_content_count": len(result.contents)},
    )


def mcp_get_prompt_result_from_sdk(result: types.GetPromptResult) -> McpToolResult:
    payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return McpToolResult(
        output=text,
        artifacts=(
            McpContentArtifact(
                role="prompt",
                media_type="application/json",
                text=text,
                metadata={"mcp_content_kind": "prompt"},
            ),
        ),
        metadata={"mcp_result_type": "GetPromptResult", "mcp_message_count": len(result.messages)},
    )


def _append_content(
    item: types.ContentBlock,
    output_parts: list[str],
    artifacts: list[McpContentArtifact],
    *,
    role_prefix: str,
) -> None:
    if isinstance(item, types.TextContent):
        output_parts.append(item.text)
        return
    if isinstance(item, types.ImageContent):
        data = _decode_base64(item.data)
        artifacts.append(
            McpContentArtifact(
                role=f"{role_prefix}_image",
                media_type=item.mime_type,
                data=data,
                metadata={"mcp_content_kind": "image"},
            )
        )
        output_parts.append(f"[image:{item.mime_type}] {len(data)} bytes archived")
        return
    if isinstance(item, types.AudioContent):
        data = _decode_base64(item.data)
        artifacts.append(
            McpContentArtifact(
                role=f"{role_prefix}_audio",
                media_type=item.mime_type,
                data=data,
                metadata={"mcp_content_kind": "audio"},
            )
        )
        output_parts.append(f"[audio:{item.mime_type}] {len(data)} bytes archived")
        return
    if isinstance(item, types.ResourceLink):
        payload = item.model_dump(mode="json", by_alias=True, exclude_none=True)
        output_parts.append("[resource_link]\n" + json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if isinstance(item, types.EmbeddedResource):
        resource = item.resource
        if isinstance(resource, types.TextResourceContents):
            output_parts.append(f"[embedded_resource:{resource.uri}]\n{resource.text}")
            artifacts.append(
                McpContentArtifact(
                    role=f"{role_prefix}_embedded_resource",
                    media_type=resource.mime_type or "text/plain; charset=utf-8",
                    text=resource.text,
                    metadata={"uri": resource.uri, "mcp_content_kind": "embedded_text_resource"},
                )
            )
        else:
            data = _decode_base64(resource.blob)
            artifacts.append(
                McpContentArtifact(
                    role=f"{role_prefix}_embedded_resource",
                    media_type=resource.mime_type or "application/octet-stream",
                    data=data,
                    metadata={"uri": resource.uri, "mcp_content_kind": "embedded_blob_resource"},
                )
            )
            output_parts.append(f"[embedded_resource_blob:{resource.uri}] {len(data)} bytes archived")


def _decode_base64(value: str) -> bytes:
    try:
        return base64.b64decode(value)
    except Exception:
        return value.encode("utf-8", errors="replace")


def _safe_protocol_version(client: Client) -> str | None:
    try:
        return client.protocol_version
    except Exception:
        return None


def _safe_sdk_generation_id(connection: _SdkServerConnection) -> str:
    generation = connection.client_generation
    if generation is None or not generation.accepting_operations:
        raise RuntimeError("MCP operation lacks an accepting client generation")
    return generation.generation_id


def _generation() -> int:
    return int(time.time() * 1000)


def _is_missing_auth(config: McpServerConfig, exc: Exception) -> bool:
    return isinstance(config.transport, McpStreamableHttpConfig) and "missing bearer token" in str(exc)


def _redact_diagnostic(message: str, config: McpServerConfig) -> str:
    transport = config.transport
    redacted = message
    if isinstance(transport, McpStreamableHttpConfig):
        redacted_url = _redact_url(transport.url)
        redacted = redacted.replace(transport.url, redacted_url)
        parsed = urlsplit(transport.url)
        if parsed.username or parsed.password:
            userinfo = parsed.netloc.rsplit("@", 1)[0]
            if userinfo:
                redacted = redacted.replace(userinfo, "<redacted-userinfo>")
        for value in transport.headers.values():
            if value:
                redacted = redacted.replace(value, "<redacted>")
        for env_var in [transport.bearer_token_env_var, *transport.env_headers.values()]:
            if env_var:
                token = os.getenv(env_var)
                if token:
                    redacted = redacted.replace(token, "<redacted>")
    if isinstance(transport, McpStdioConfig):
        for value in transport.env.values():
            if value:
                redacted = redacted.replace(value, "<redacted>")
    return redacted


def _redact_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    if parsed.username or parsed.password:
        host = f"<redacted-userinfo>@{host}"
    suffix = "<redacted-query-or-fragment>" if parsed.query or parsed.fragment else ""
    return urlunsplit(
        SplitResult(
            scheme=parsed.scheme,
            netloc=host,
            path=parsed.path,
            query="",
            fragment="",
        )
    ) + suffix
