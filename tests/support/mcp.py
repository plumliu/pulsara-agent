"""Deterministic test-owned MCP manager."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic
from typing import TYPE_CHECKING, Any

from pulsara_agent.ports.mcp import (
    McpInvocationOwner,
    McpConfirmedContinuationDispatchReceipt,
)
from pulsara_agent.ports.mcp_secret import (
    McpContinuationSecretBorrowIssuer,
    McpReplayReadyCarrierPlaintext,
    McpSecretAccessPurpose,
    build_retryable_tool_call_payload,
)
from pulsara_agent.event import EventContext
from pulsara_agent.primitives._context_base import freeze_json
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.mcp import McpServerLifecycleTimingFact
from pulsara_agent.primitives.mcp_continuation import (
    default_mcp_continuation_bounds,
)
from pulsara_agent.primitives.mcp_protocol import (
    McpAuthAttributionFact,
    McpCacheableMethod,
    McpCachePageAttributionFact,
    McpClientCapabilityPolicyFact,
    McpClientInputMethod,
    McpDiscoveryAttributionFact,
    McpDiscoveryPageSetAttributionFact,
    McpElicitationMode,
    McpEndpointAttributionFact,
    McpFinalDiscoverWireReceiptFact,
    McpProtocolBehaviorEra,
    McpProtocolNegotiationAttributionFact,
    McpServerProtocolSemanticFact,
    McpServerSnapshotAuthorityFact,
    McpServerSurfaceSemanticFact,
    McpToolDiscoveryAttributionFact,
    build_mcp_protocol_fact,
)
from pulsara_agent.runtime.mcp.continuation_store import (
    InMemoryMcpContinuationSecretStore,
    McpContinuationKeyProvider,
    McpContinuationSecretCodec,
)
from pulsara_agent.runtime.mcp.protocol import (
    McpClientInputRequired,
    lower_input_required_result,
)
from pulsara_agent.runtime.mcp.schema import (
    MCP_OUTPUT_VALIDATION_CONTRACT_FINGERPRINT,
    MCP_SDK_CONFORMANCE_CONTRACT_FINGERPRINT,
    build_conformed_tool_schema,
)
from pulsara_agent.runtime.mcp.tool_execution_port import _prepare_suspension
from pulsara_agent.runtime.mcp.types import (
    McpDiscoveredTool,
    McpServerCandidate,
    McpServerRuntimeSpec,
    McpServerSnapshot,
    McpServerStatus,
    McpManagerLease,
    McpToolAnnotations,
    event_safe_mcp_config_fingerprint,
    new_mcp_slot,
    runtime_mcp_config_fingerprint,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession


McpToolHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]


def make_mcp_client_input_required(
    *,
    interaction_id: str,
    server_id: str = "docs",
    tool_name: str = "lookup",
    request_key: str = "answer",
    request_state: str | None = "opaque-test-state",
    round_ordinal: int = 1,
) -> McpClientInputRequired:
    codec = McpContinuationSecretCodec(
        McpContinuationKeyProvider.from_master_key(
            key_id="test-mcp-continuation-key",
            master_key=b"pulsara-test-mcp-continuation-key" * 2,
        )
    )
    retryable = build_retryable_tool_call_payload(
        tool_name=tool_name,
        arguments={},
        source_method_schema_fingerprint=context_fingerprint(
            "test-mcp-retryable-method-schema:v1", "tools/call"
        ),
    )
    bounds = default_mcp_continuation_bounds()
    leg, private_urls = lower_input_required_result(
        input_requests={
            request_key: {
                "method": "elicitation/create",
                "params": {
                    "mode": "form",
                    "message": "Provide a test value",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
            }
        },
        request_state=request_state,
        leg_ordinal=round_ordinal,
        retryable_payload=retryable,
        operation_deadline_monotonic=monotonic() + 300.0,
        commitment_key_id=codec.key_id,
        keyed_commitment=codec.keyed_commitment,
        elicitation_advertised=True,
        bounds=bounds,
    )
    return McpClientInputRequired(
        interaction_id=interaction_id,
        server_id=server_id,
        exact_protocol_revision="2026-07-28",
        protocol_semantic_fingerprint=context_fingerprint(
            "test-mcp-protocol-semantic:v1", "2026-07-28"
        ),
        endpoint_attribution_fingerprint=context_fingerprint(
            "test-mcp-endpoint-attribution:v1", server_id
        ),
        auth_attribution_fingerprint=context_fingerprint(
            "test-mcp-auth-attribution:v1", server_id
        ),
        sdk_client_generation_id="test-mcp-sdk-generation:1",
        leg=leg,
        retryable_request_payload=retryable,
        private_url_payloads=private_urls,
        continuation_bounds=bounds,
        first_input_required_observed_at_utc=(
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ),
    )


@dataclass(frozen=True, slots=True)
class PreparedTestMcpInputRequiredSuspension:
    """Test-owned carrier around the real v2 secure suspension factory."""

    prepared: object = field(repr=False)
    codec: McpContinuationSecretCodec = field(repr=False)
    repository: InMemoryMcpContinuationSecretStore = field(repr=False)

    def __reduce__(self):
        raise TypeError("test MCP suspensions are process-local")

    def __getattr__(self, name: str) -> object:
        return getattr(self.prepared, name)


def prepare_test_mcp_input_required_suspension(
    *,
    interaction_id: str,
    runtime_session_id: str,
    run_id: str,
    turn_id: str,
    reply_id: str,
    tool_call_id: str,
    tool_name: str,
    server_id: str,
    binding_identity,
    pending_lease_reservation_id: str,
    protocol_revision: str = "2026-07-28",
    request_state: str | None = "opaque-test-state",
) -> PreparedTestMcpInputRequiredSuspension:
    codec = McpContinuationSecretCodec(
        McpContinuationKeyProvider.from_master_key(
            key_id="test-mcp-continuation-key",
            master_key=b"pulsara-test-mcp-continuation-key" * 2,
        )
    )
    repository = InMemoryMcpContinuationSecretStore()
    result = make_mcp_client_input_required(
        interaction_id=interaction_id,
        server_id=server_id,
        tool_name=tool_name.removeprefix(f"mcp__{server_id}__"),
        request_state=request_state,
    )
    if result.exact_protocol_revision != protocol_revision:
        raise ValueError("test MCP protocol revision is unsupported")
    prepared = _prepare_suspension(
        codec=codec,
        repository=repository,
        issuer_id="test-mcp-tool-execution-port",
        result=result,
        owner=McpInvocationOwner(
            runtime_session_id=runtime_session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            event_context=EventContext(
                run_id=run_id,
                turn_id=turn_id,
                reply_id=reply_id,
            ),
        ),
        exposed_tool_name=tool_name,
        binding=binding_identity,
        binding_contract_fingerprint=context_fingerprint(
            "test-mcp-binding-contract:v1",
            binding_identity.model_dump(mode="json"),
        ),
        reservation_id=pending_lease_reservation_id,
        predecessor_resolution_reference=None,
        inherited_expiry=None,
        source_dispatch_record=None,
        browser_port=None,
    )
    return PreparedTestMcpInputRequiredSuspension(
        prepared=prepared,
        codec=codec,
        repository=repository,
    )


def install_prepared_test_mcp_pending_handle(
    *,
    runtime_session: RuntimeSession,
    supervisor: object,
    prepared: PreparedTestMcpInputRequiredSuspension,
    event_context: EventContext,
):
    """Install a production port around one already prepared test suspension."""

    from pulsara_agent.runtime.mcp.tool_execution_port import (
        RuntimeMcpToolExecutionPort,
    )

    port = RuntimeMcpToolExecutionPort(
        supervisor,  # type: ignore[arg-type]
        continuation_codec=prepared.codec,
        continuation_repository=prepared.repository,
    )
    runtime_session.mcp_supervisor = supervisor
    runtime_session.mcp_tool_execution_port = port
    handle = port._new_handle(
        owner=McpInvocationOwner(
            runtime_session_id=runtime_session.runtime_session_id,
            run_id=event_context.run_id,
            tool_call_id=prepared.prepared.interaction.tool_call_id,
            event_context=event_context,
        ),
        prepared=prepared.prepared,
        predecessor=None,
    )
    return handle


@dataclass(slots=True)
class MockMcpClientManager:
    _snapshots: tuple[McpServerSnapshot, ...]
    handlers: dict[tuple[str, str], McpToolHandler] = field(default_factory=dict)
    close_count: int = 0
    cancel_count: int = 0
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    _closed: bool = False
    _active_tasks: set[asyncio.Task[Any]] = field(
        default_factory=set, init=False, repr=False
    )

    @property
    def snapshots(self) -> tuple[McpServerSnapshot, ...]:
        return self._snapshots

    @property
    def closed(self) -> bool:
        return self._closed

    async def call_tool(
        self,
        binding_lease: McpManagerLease,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_ms: int,
    ) -> Any:
        if self._closed:
            raise RuntimeError("MCP manager is closed")
        server_id = binding_lease.binding_identity.server_id
        self.calls.append((server_id, tool_name, dict(arguments)))
        handler = self.handlers.get((server_id, tool_name))
        if handler is None:
            raise KeyError(f"Unknown MCP tool binding: {server_id}/{tool_name}")
        task = asyncio.create_task(_await_handler(handler, dict(arguments)))
        self._active_tasks.add(task)
        try:
            return await asyncio.wait_for(task, timeout=timeout_ms / 1000)
        finally:
            self._active_tasks.discard(task)

    def cancel_active(self) -> None:
        self.cancel_count += 1
        for task in tuple(self._active_tasks):
            task.cancel()

    def activate_subscription(self) -> None:
        """Mock snapshots have no independent subscription transport."""

    async def resume_suspended_request(
        self,
        *,
        binding_lease: McpManagerLease,
        replay_plaintext: McpReplayReadyCarrierPlaintext,
        dispatch_receipt: McpConfirmedContinuationDispatchReceipt,
        timeout_ms: int,
    ) -> Any:
        borrow = McpContinuationSecretBorrowIssuer("test-mcp-manager-wire").issue(
            McpSecretAccessPurpose.FRESH_WIRE_BUILD
        )
        try:
            base, _request_state, _responses = borrow.wire_retry_parts(replay_plaintext)
        finally:
            borrow.revoke()
        if dispatch_receipt.interaction_id == "":
            raise RuntimeError("mock MCP dispatch receipt is incomplete")
        if base.get("source_method") != "tools/call":
            raise RuntimeError("mock MCP manager only resumes tool calls")
        tool_name = base.get("tool_name")
        arguments = base.get("arguments")
        if not isinstance(tool_name, str) or not isinstance(arguments, dict):
            raise RuntimeError("mock MCP replay payload is malformed")
        return await self.call_tool(
            binding_lease,
            tool_name,
            arguments,
            timeout_ms=timeout_ms,
        )

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        self.close_count += 1
        self.cancel_active()
        if self._active_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tuple(self._active_tasks), return_exceptions=True),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                pass


async def queue_ready_test_mcp_candidate(
    supervisor: object,
    runtime: object,
    *,
    tool_name: str = "lookup",
    handler: McpToolHandler,
) -> MockMcpClientManager:
    """Queue one deterministic READY candidate through Supervisor ownership."""

    config = runtime.spec.config  # type: ignore[attr-defined]
    snapshot_id = f"mcp_snapshot:{runtime.attempt.reconcile_attempt_id}"  # type: ignore[attr-defined]
    conformed = build_conformed_tool_schema(
        server_id=config.server_id,
        name=tool_name,
        title=None,
        description=f"test MCP tool {tool_name}",
        input_schema={"type": "object", "properties": {}},
        output_schema=None,
        annotations={"readOnlyHint": True},
    )
    listing_generation_fingerprint = context_fingerprint(
        "test-mcp-sdk-listing-generation:v1",
        {
            "snapshot_id": snapshot_id,
            "tool": conformed.semantic.tool_semantic_fingerprint,
        },
    )
    tools_page = build_mcp_protocol_fact(
        McpCachePageAttributionFact,
        schema_version="mcp_cache_page_attribution.v1",
        method=McpCacheableMethod.TOOLS_LIST,
        request_params_fingerprint=context_fingerprint(
            "mcp-cache-page-request-params:v1",
            {"method": "tools/list", "cursor": None},
        ),
        request_cursor=None,
        page_ordinal=0,
        received_at_utc="2026-01-01T00:00:00.000000Z",
        raw_ttl_ms=None,
        resolved_ttl_ms=0,
        raw_cache_scope=None,
        resolved_cache_scope="private",
        hint_disposition="absent_earlier_revision",
        result_payload_fingerprint=context_fingerprint(
            "mcp-cache-page-result:v1",
            {
                "tools": (conformed.semantic.tool_semantic_fingerprint,),
                "nextCursor": None,
            },
        ),
        next_cursor=None,
    )
    tools_page_set = build_mcp_protocol_fact(
        McpDiscoveryPageSetAttributionFact,
        schema_version="mcp_discovery_page_set_attribution.v1",
        method=McpCacheableMethod.TOOLS_LIST,
        started_from_cursor_none=True,
        ordered_pages=(tools_page,),
        page_receipt_accumulator=context_fingerprint(
            "mcp-discovery-page-receipt-accumulator:v1",
            (tools_page.page_receipt_fingerprint,),
        ),
        common_resolved_cache_scope="private",
        complete_capture=True,
    )
    tool_attribution = build_mcp_protocol_fact(
        McpToolDiscoveryAttributionFact,
        schema_version="mcp_tool_discovery_attribution.v1",
        tool_semantic_fingerprint=conformed.semantic.tool_semantic_fingerprint,
        source_page_receipt_fingerprint=tools_page.page_receipt_fingerprint,
        sdk_conformance_contract_fingerprint=(MCP_SDK_CONFORMANCE_CONTRACT_FINGERPRINT),
        sdk_conformed_listing_generation_fingerprint=(listing_generation_fingerprint),
        sdk_header_routing_contract_fingerprint=(
            MCP_SDK_CONFORMANCE_CONTRACT_FINGERPRINT
        ),
        pulsara_output_validation_contract_fingerprint=(
            MCP_OUTPUT_VALIDATION_CONTRACT_FINGERPRINT
        ),
    )
    discovered = McpDiscoveredTool(
        server_id=config.server_id,
        name=tool_name,
        description=f"test MCP tool {tool_name}",
        input_schema={"type": "object", "properties": {}},
        annotations=McpToolAnnotations(read_only_hint=True),
        semantic=conformed.semantic,
        discovery_attribution=tool_attribution,
        provider_projection=conformed.provider_projection,
    )
    endpoint = build_mcp_protocol_fact(
        McpEndpointAttributionFact,
        schema_version="mcp_endpoint_attribution.v1",
        transport_kind="stdio",
        canonical_target_fingerprint=context_fingerprint(
            "test-mcp-stdio-target:v1", config.server_id
        ),
        tls_policy_fingerprint=None,
        redirect_policy="deny",
        executable_identity_fingerprint=context_fingerprint(
            "test-mcp-executable:v1", config.server_id
        ),
    )
    auth = build_mcp_protocol_fact(
        McpAuthAttributionFact,
        schema_version="mcp_auth_attribution.v1",
        auth_kind="none",
        issuer_identity_fingerprint=None,
        client_identity_fingerprint=None,
        effective_scope_fingerprint=None,
        credential_generation=0,
        keyed_credential_commitment=None,
    )
    capability_policy = build_mcp_protocol_fact(
        McpClientCapabilityPolicyFact,
        schema_version="mcp_client_capability_policy.v1",
        supported_input_methods=(McpClientInputMethod.ELICITATION_CREATE,),
        elicitation_modes=(McpElicitationMode.FORM, McpElicitationMode.URL),
        elicitation_host_contract_fingerprint=context_fingerprint(
            "test-mcp-elicitation-host-contract:v1", config.server_id
        ),
        sampling_advertised=False,
        roots_advertised=False,
        logging_advertised=False,
        ordered_extension_ads=(),
    )
    protocol = build_mcp_protocol_fact(
        McpServerProtocolSemanticFact,
        schema_version="mcp_server_protocol_semantic.v1",
        protocol_revision="2026-07-28",
        behavior_era=McpProtocolBehaviorEra.STATELESS_PER_REQUEST,
        server_capabilities=freeze_json({}),
        ordered_extension_contracts=(),
    )
    wire_receipt = build_mcp_protocol_fact(
        McpFinalDiscoverWireReceiptFact,
        schema_version="mcp_final_discover_wire_receipt.v1",
        physical_operation_id=f"test-mcp-discover:{snapshot_id}",
        sdk_client_generation_id=f"test-mcp-sdk-generation:{snapshot_id}",
        exact_protocol_revision=protocol.protocol_revision,
        client_capability_policy_fingerprint=capability_policy.policy_fingerprint,
        endpoint_attribution_fingerprint=endpoint.attribution_fingerprint,
        auth_attribution_fingerprint=auth.attribution_fingerprint,
        raw_result_payload_fingerprint=context_fingerprint(
            "test-mcp-discover-result:v1", snapshot_id
        ),
    )
    negotiation = build_mcp_protocol_fact(
        McpProtocolNegotiationAttributionFact,
        schema_version="mcp_protocol_negotiation_attribution.v1",
        protocol_semantic_fingerprint=protocol.semantic_fingerprint,
        client_capability_policy_fingerprint=capability_policy.policy_fingerprint,
        negotiation_source="server_discover",
        negotiation_wire_receipt_fingerprint=wire_receipt.receipt_fingerprint,
        sdk_version="2.0.0",
        sdk_conformance_contract_fingerprint=(MCP_SDK_CONFORMANCE_CONTRACT_FINGERPRINT),
        server_info=freeze_json({}),
        endpoint_attribution_fingerprint=endpoint.attribution_fingerprint,
        auth_attribution_fingerprint=auth.attribution_fingerprint,
    )
    surface = build_mcp_protocol_fact(
        McpServerSurfaceSemanticFact,
        schema_version="mcp_server_surface_semantic.v1",
        server_id=config.server_id,
        protocol_semantic=protocol,
        tools=(conformed.semantic,),
        resources=(),
        resource_templates=(),
        prompts=(),
        instructions=None,
    )
    discovery = build_mcp_protocol_fact(
        McpDiscoveryAttributionFact,
        schema_version="mcp_discovery_attribution.v1",
        snapshot_id=snapshot_id,
        config_epoch=runtime.attempt.config_epoch,  # type: ignore[attr-defined]
        discovery_generation=runtime.attempt.reserved_discovery_generation,  # type: ignore[attr-defined]
        transport_generation=1,
        endpoint=endpoint,
        auth=auth,
        negotiation=negotiation,
        page_set_receipts=(tools_page_set,),
        ordered_tool_rejections=(),
        reconcile_attempt_id=runtime.attempt.reconcile_attempt_id,  # type: ignore[attr-defined]
    )
    authority = build_mcp_protocol_fact(
        McpServerSnapshotAuthorityFact,
        schema_version="mcp_server_snapshot_authority.v1",
        surface_semantic=surface,
        discovery_attribution=discovery,
        ordered_provider_projections=(conformed.provider_projection,),
        surface_semantic_fingerprint=surface.surface_semantic_fingerprint,
        projection_accumulator=context_fingerprint(
            "test-mcp-provider-projection-accumulator:v1",
            (conformed.provider_projection.projection_fingerprint,),
        ),
    )
    timing = McpServerLifecycleTimingFact(
        queued_at_utc=runtime.queued_at_utc,  # type: ignore[attr-defined]
        connect_started_at_utc="2026-01-01T00:00:00Z",
        connect_ended_at_utc="2026-01-01T00:00:00Z",
        discovery_started_at_utc="2026-01-01T00:00:00Z",
        discovery_ended_at_utc="2026-01-01T00:00:00.010000Z",
        completed_at_utc="2026-01-01T00:00:00.010000Z",
        connect_duration_seconds=0,
        discovery_duration_seconds=0.01,
        total_duration_seconds=0.01,
    )
    snapshot = McpServerSnapshot(
        snapshot_id=snapshot_id,
        server_id=config.server_id,
        config_epoch=runtime.attempt.config_epoch,  # type: ignore[attr-defined]
        event_safe_config_fingerprint=event_safe_mcp_config_fingerprint(config),
        snapshot_semantic_fingerprint=authority.surface_semantic_fingerprint,
        reconcile_attempt_id=runtime.attempt.reconcile_attempt_id,  # type: ignore[attr-defined]
        discovery_generation=runtime.attempt.reserved_discovery_generation,  # type: ignore[attr-defined]
        status=McpServerStatus.READY,
        required=config.required,
        tools=(discovered,),
        protocol_version=protocol.protocol_revision,
        authority=authority,
        timing=timing,
    )
    manager = MockMcpClientManager(
        _snapshots=(snapshot,),
        handlers={(config.server_id, tool_name): handler},
    )
    spec = McpServerRuntimeSpec(
        config=config,
        runtime_config_fingerprint=runtime_mcp_config_fingerprint(config),
        event_safe_config_fingerprint=event_safe_mcp_config_fingerprint(config),
    )
    candidate = McpServerCandidate(
        ticket_id=runtime.ticket_id,  # type: ignore[attr-defined]
        config_epoch=runtime.attempt.config_epoch,  # type: ignore[attr-defined]
        reconcile_attempt_id=runtime.attempt.reconcile_attempt_id,  # type: ignore[attr-defined]
        reserved_discovery_generation=runtime.attempt.reserved_discovery_generation,  # type: ignore[attr-defined]
        server_snapshot=snapshot,
        runtime_spec=spec,
        manager_slot=new_mcp_slot(spec=spec, snapshot=snapshot, manager=manager),
        trigger=runtime.trigger,  # type: ignore[attr-defined]
        request_count=1,
        page_count=1,
    )
    with supervisor._state_lock:  # type: ignore[attr-defined]
        current = supervisor._current_attempts.get(config.server_id)  # type: ignore[attr-defined]
        if current is runtime:
            supervisor._candidates.append(candidate)  # type: ignore[attr-defined]
    return manager


async def _await_handler(handler: McpToolHandler, arguments: dict[str, Any]) -> Any:
    result = handler(arguments)
    if hasattr(result, "__await__"):
        return await result  # type: ignore[misc]
    return result


__all__ = [
    "MockMcpClientManager",
    "PreparedTestMcpInputRequiredSuspension",
    "install_prepared_test_mcp_pending_handle",
    "prepare_test_mcp_input_required_suspension",
    "queue_ready_test_mcp_candidate",
    "make_mcp_client_input_required",
]
