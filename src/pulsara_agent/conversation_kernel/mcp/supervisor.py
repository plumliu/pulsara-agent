"""Host-scoped MCP physical owner, discovery, fencing, and exact execution."""

from __future__ import annotations

import asyncio
import random
from contextlib import suppress
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Awaitable, Callable, Mapping

from pulsara_agent.capability.contracts import (
    CapabilityKind,
    CapabilitySourceKind,
    CapabilitySourceRefreshMode,
    CapabilitySourceSnapshotDisposition,
    FrozenMcpCapabilityProjectionInput,
    FrozenToolCapabilityFact,
    McpInspectEffectKind,
    McpProviderNameCollisionFact,
    McpProviderNameCollisionMember,
    McpToolCapabilityRef,
    ToolCapabilityOrigin,
    capability_identity,
    capability_source_ref,
    capability_source_registration,
    freeze_capability_source_snapshot,
    freeze_mcp_inspectability_fact,
    freeze_tool_capability_fact,
    tool_capability_version_ref,
)
from pulsara_agent.capability.mcp_projection import (
    McpInspectionDescriptorValues,
    conservative_mcp_inspection_logical_utf8_bytes,
    mcp_inspection_descriptor_payload_fingerprint,
)
from pulsara_agent.conversation_kernel.capability_composition import (
    PreparedMcpCapabilitySourceSnapshotSet,
    PreparedMcpInspectionInput,
    issue_mcp_capability_source_snapshot_set,
)
from pulsara_agent.identity import random_id
from pulsara_agent.mcp_config import (
    LocalConfiguredMcpRuntimeSource,
    ManagedPackageMcpRuntimeSource,
    McpScopePolicy,
    McpServerConfig,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import (
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary

from ..tool_surface import (
    McpEffectKind,
    McpToolExecutionPolicyFact,
    PreparedToolExecutionBinding,
    execution_policy_fingerprint,
)
from .contracts import (
    MAXIMUM_CONFIGURED_MCP_SERVERS,
    MAXIMUM_MCP_HOST_IN_FLIGHT,
    MAXIMUM_MCP_REMOTE_BODY_BYTES,
    McpCatalogSnapshot,
    McpCatalogToolInspection,
    McpConfiguredServerInspection,
    McpDiscoveryCatalogInspection,
    McpInstallationCandidate,
    McpPhysicalConcurrencyKind,
    McpProtocolConformanceError,
    McpServerCatalogEntry,
    McpServerState,
    McpToolSemanticFact,
    McpTransportOperationError,
    build_catalog_snapshot,
    mcp_prompt_public_item,
    scope_mcp_discovery_snapshot,
)
from .input_required import (
    McpInputRequiredRoundOwner,
    McpInputRequiredUnsupported,
)
from .schema import McpOutputSchemaMismatch

if TYPE_CHECKING:
    from .sdk_facade import BoundedMcpSdkClient, types
from .discovery import (
    _DEFAULT_MCP_SCHEMA_DIALECT as _DEFAULT_MCP_SCHEMA_DIALECT,
    _SUPPORTED_MCP_SCHEMA_DIALECTS as _SUPPORTED_MCP_SCHEMA_DIALECTS,
    _assert_catalog_item_bound as _assert_catalog_item_bound,
    _bounded_public_identity as _bounded_public_identity,
    _candidate as _candidate,
    _discovery_snapshot_physical_bytes as _discovery_snapshot_physical_bytes,
    _DiscoveryBudget as _DiscoveryBudget,
    _effect as _effect,
    _list_optional_resource_templates as _list_optional_resource_templates,
    _list_pages as _list_pages,
    _mcp_schema_dialect as _mcp_schema_dialect,
    _prompt as _prompt,
    _resource as _resource,
    _resource_template as _resource_template,
    _sanitize_text as _sanitize_text,
    discover_mcp_catalog as discover_mcp_catalog,
)
from .resource_uris import (
    _URI_TEMPLATE_VARIABLE as _URI_TEMPLATE_VARIABLE,
    _match_named_uri_expansion as _match_named_uri_expansion,
    _match_uri_template_expansion as _match_uri_template_expansion,
    _parse_resource_uri_template as _parse_resource_uri_template,
    _resource_uri_in_snapshot as _resource_uri_in_snapshot,
    _resource_uri_matches_template as _resource_uri_matches_template,
    _standard_operation_target as _standard_operation_target,
    _standard_resource_uri as _standard_resource_uri,
    _uri_template_variable_name as _uri_template_variable_name,
)
from .results import (
    McpKnownToolResult as McpKnownToolResult,
    _known_mcp_remote_error as _known_mcp_remote_error,
    _known_mcp_system_failure as _known_mcp_system_failure,
    _mcp_error_is_transport_failure as _mcp_error_is_transport_failure,
    _mcp_invocation_failure as _mcp_invocation_failure,
    _mcp_remote_identity as _mcp_remote_identity,
    _mcp_standard_operation_identity as _mcp_standard_operation_identity,
    mcp_failure_required_facts as mcp_failure_required_facts,
)
from .standard_operations import (
    _get_prompt_result as _get_prompt_result,
    _invoke_standard_remote_result as _invoke_standard_remote_result,
    _remaining_mcp_timeout as _remaining_mcp_timeout,
    user_prompt_text as user_prompt_text,
)
from .slots import (
    McpConnectionSlot as McpConnectionSlot,
    McpDispatchAdmissionPermit as McpDispatchAdmissionPermit,
    McpDispatchFenceState as McpDispatchFenceState,
    McpDispatchPermitState as McpDispatchPermitState,
    McpPhysicalOperationPermit as McpPhysicalOperationPermit,
    McpSlotLease as McpSlotLease,
    McpSnapshotStale as McpSnapshotStale,
    _acquire_dispatch_lane as _acquire_dispatch_lane,
    _close_physical_lifetime_anchor as _close_physical_lifetime_anchor,
    _McpDispatchCancelledBeforeStart as _McpDispatchCancelledBeforeStart,
)
from .wire import DEFAULT_MCP_WIRE_BOUNDS


async def _prepare_sdk_client_factory() -> type[BoundedMcpSdkClient]:
    # A background import may hold Python's module lock. Keep it off the HTTP
    # loop, and join the physical worker even if this connection is cancelled.
    work = asyncio.create_task(asyncio.to_thread(import_module, __package__ + ".sdk_facade"))
    try:
        module = await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        with suppress(BaseException):
            work.result()
        raise
    return module.BoundedMcpSdkClient


@dataclass(frozen=True, slots=True)
class _McpConnectOutcome:
    """Result owned by the existing physical connection-attempt task."""

    state: McpServerState
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class McpBoundToolExecutor:
    runtime_generation_id: int
    semantic: McpToolSemanticFact
    policy: McpToolExecutionPolicyFact
    lease: McpSlotLease = field(repr=False, compare=False)

    def admit(
        self,
        *,
        session_id: str,
        scope_kind: ModelInputScopeKind,
        turn_id: str,
        tool_call_id: str,
    ) -> McpDispatchAdmissionPermit:
        return self.lease._slot.admit_dispatch(  # noqa: SLF001
            lease=self.lease,
            session_id=session_id,
            scope_kind=scope_kind,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            descriptor_fingerprint=self.semantic.descriptor_fingerprint,
            policy_fingerprint=execution_policy_fingerprint(self.policy),
            runtime_generation_id=self.runtime_generation_id,
        )

    async def invoke(
        self,
        permit: McpDispatchAdmissionPermit,
        arguments: Mapping[str, object],
        *, cell_cancel_event: asyncio.Event | None = None,
    ) -> McpKnownToolResult:
        from .sdk_facade import MCPError, types

        if (
            permit.lease is not self.lease
            or permit.runtime_generation_id != self.runtime_generation_id
            or permit.descriptor_fingerprint != self.semantic.descriptor_fingerprint
            or permit.policy_fingerprint != execution_policy_fingerprint(self.policy)
            or permit.tool_call_id == ""
        ):
            raise RuntimeError("MCP dispatch permit does not exact-join executor")
        remote_identity, remote_identity_verification = _mcp_remote_identity(
            semantic=self.semantic, connection_generation=permit.lease.connection_generation
        )
        try:
            operation = await permit.acquire_operation(cell_cancel_event=cell_cancel_event)
        except _McpDispatchCancelledBeforeStart:
            permit.release()
            return McpKnownToolResult(
                "CANCELLED_BEFORE_DISPATCH", "Cell 已停止".encode("utf-8"),
                remote_identity, remote_identity_verification,
            )
        except BaseException:
            if permit.state is McpDispatchPermitState.ATTEMPT_ACCEPTED:
                permit.release()
            raise
        async with operation:
            input_owner = McpInputRequiredRoundOwner(
                operation_identity=permit.permit_id,
                connection_generation=self.lease.connection_generation,
            )
            try:
                operation_deadline = (
                    asyncio.get_running_loop().time()
                    + self.policy.timeout_ms / 1000
                )
                try:
                    result = await self.lease._slot.client.session.call_tool(  # noqa: SLF001
                        self.semantic.remote_tool_name,
                        arguments=dict(arguments),
                        read_timeout_seconds=_remaining_mcp_timeout(
                            operation_deadline
                        ),
                        allow_input_required=True,
                    )
                    result_type = self.lease._slot.client.require_closed_result_type(  # noqa: SLF001
                        result
                    )
                    while result_type == "input_required":
                        if not isinstance(result, types.InputRequiredResult):
                            raise McpProtocolConformanceError(
                                "MCP_RESULT_TYPE_PAYLOAD_CONTRADICTION"
                            )
                        try:
                            request_state = (
                                input_owner.prepare_state_only_continuation(result)
                            )
                        except McpInputRequiredUnsupported as exc:
                            if self.policy.effect_kind is McpEffectKind.READ_ONLY:
                                return _known_mcp_system_failure(
                                    error_code="MCP_INPUT_REQUIRED_UNSUPPORTED",
                                    remote_identity=remote_identity,
                                    remote_identity_verification=remote_identity_verification,
                                    message=self.lease._slot.client.diagnostic_message(exc),  # noqa: SLF001
                                )
                            return _mcp_invocation_failure(
                                error=exc,
                                client=self.lease._slot.client,  # noqa: SLF001
                                remote_identity=remote_identity,
                                remote_identity_verification=remote_identity_verification,
                                effect_kind=self.policy.effect_kind,
                            )
                        result = await self.lease._slot.client.session.call_tool(  # noqa: SLF001
                            self.semantic.remote_tool_name,
                            arguments=dict(arguments),
                            input_responses={},
                            request_state=request_state,
                            read_timeout_seconds=_remaining_mcp_timeout(
                                operation_deadline
                            ),
                            allow_input_required=True,
                        )
                        result_type = (
                            self.lease._slot.client.require_closed_result_type(  # noqa: SLF001
                                result
                            )
                        )
                except McpOutputSchemaMismatch as exc:
                    return _known_mcp_system_failure(
                        error_code="MCP_OUTPUT_SCHEMA_MISMATCH",
                        remote_identity=remote_identity,
                        remote_identity_verification=remote_identity_verification,
                        message=self.lease._slot.client.diagnostic_message(exc),  # noqa: SLF001
                    )
                except McpProtocolConformanceError as exc:
                    self.lease._slot.report_transport_failure(  # noqa: SLF001
                        "MCP_PROTOCOL_CONFORMANCE_FAILED", retryable=False
                    )
                    return _known_mcp_system_failure(
                        error_code="MCP_RESULT_TYPE_CONFORMANCE_FAILED",
                        remote_identity=remote_identity,
                        remote_identity_verification=remote_identity_verification,
                        message=self.lease._slot.client.diagnostic_message(exc),  # noqa: SLF001
                    )
                except McpTransportOperationError as exc:
                    self.lease._slot.report_transport_failure(  # noqa: SLF001
                        "MCP_TRANSPORT_FAILED", retryable=True
                    )
                    if not exc.may_have_reached_server:
                        return _known_mcp_system_failure(
                            error_code="MCP_TRANSPORT_UNWRITTEN",
                            remote_identity=remote_identity,
                            remote_identity_verification=remote_identity_verification,
                        )
                    return _mcp_invocation_failure(
                        error=exc,
                        client=self.lease._slot.client,  # noqa: SLF001
                        remote_identity=remote_identity,
                        remote_identity_verification=remote_identity_verification,
                        effect_kind=self.policy.effect_kind,
                    )
                except MCPError as exc:
                    if not _mcp_error_is_transport_failure(exc):
                        return _known_mcp_remote_error(
                            error=exc,
                            client=self.lease._slot.client,  # noqa: SLF001
                            remote_identity=remote_identity,
                            remote_identity_verification=remote_identity_verification,
                        )
                    self.lease._slot.report_transport_failure(  # noqa: SLF001
                        "MCP_TRANSPORT_FAILED", retryable=True
                    )
                    return _mcp_invocation_failure(
                        error=exc,
                        client=self.lease._slot.client,  # noqa: SLF001
                        remote_identity=remote_identity,
                        remote_identity_verification=remote_identity_verification,
                        effect_kind=self.policy.effect_kind,
                    )
                except (asyncio.CancelledError, MemoryError):
                    raise
                except Exception as exc:
                    self.lease._slot.report_transport_failure(  # noqa: SLF001
                        "MCP_TRANSPORT_FAILED", retryable=True
                    )
                    return _mcp_invocation_failure(
                        error=exc,
                        client=self.lease._slot.client,  # noqa: SLF001
                        remote_identity=remote_identity,
                        remote_identity_verification=remote_identity_verification,
                        effect_kind=self.policy.effect_kind,
                    )

                # At this boundary an exact response is already present.  Any
                # carrier/rendering failure is therefore a known protocol
                # failure, never an ambiguous physical effect and never a
                # reason to replay tools/call.
                try:
                    if not isinstance(result, types.CallToolResult):
                        raise McpProtocolConformanceError(
                            "MCP_RESULT_TYPE_PAYLOAD_CONTRADICTION"
                        )
                    body = _render_typed_result(result)
                except MemoryError:
                    raise
                except Exception as exc:
                    return _known_mcp_system_failure(
                        error_code="MCP_RESULT_LOWERING_FAILED",
                        remote_identity=remote_identity,
                        remote_identity_verification=remote_identity_verification,
                        message=self.lease._slot.client.diagnostic_message(exc),  # noqa: SLF001
                    )
                return McpKnownToolResult(
                    state="APPLICATION_ERROR" if result.is_error else "SUCCESS",
                    content=body,
                    remote_identity=remote_identity,
                    remote_identity_verification=remote_identity_verification,
                )
            except asyncio.CancelledError:
                raise
            finally:
                input_owner.close()


@dataclass(frozen=True, slots=True)
class McpInstalledRuntimeGeneration:
    runtime_generation_id: int
    root_tool_specs: tuple[McpToolSemanticFact, ...]
    subagent_tool_specs: tuple[McpToolSemanticFact, ...]
    execution_bindings: tuple[PreparedToolExecutionBinding, ...]
    executors: Mapping[str, McpBoundToolExecutor] = field(repr=False, compare=False)
    catalog_snapshot: McpCatalogSnapshot
    subagent_catalog_snapshot: McpCatalogSnapshot
    slot_leases: tuple[McpSlotLease, ...] = field(repr=False, compare=False)
    slot_lease_by_server: Mapping[str, McpSlotLease] = field(
        repr=False, compare=False
    )
    candidates: Mapping[str, McpInstallationCandidate] = field(
        repr=False, compare=False
    )
    root_provider_name_collision_facts: tuple[
        McpProviderNameCollisionFact, ...
    ] = ()
    subagent_provider_name_collision_facts: tuple[
        McpProviderNameCollisionFact, ...
    ] = ()

    def catalog_for_scope(
        self, scope: ModelInputScopeKind
    ) -> McpCatalogSnapshot:
        return (
            self.catalog_snapshot
            if scope is ModelInputScopeKind.ROOT
            else self.subagent_catalog_snapshot
        )

    def provider_name_collisions_for_scope(
        self, scope: ModelInputScopeKind
    ) -> tuple[McpProviderNameCollisionFact, ...]:
        return (
            self.root_provider_name_collision_facts
            if scope is ModelInputScopeKind.ROOT
            else self.subagent_provider_name_collision_facts
        )

    def release(self) -> None:
        for lease in self.slot_leases:
            lease.release()

    def standard_operation_server_id(
        self, tool_name: str, arguments: Mapping[str, object]
    ) -> str | None:
        if tool_name in {
            "read_mcp_resource",
        }:
            value = _standard_operation_target(tool_name, arguments)["server_id"]
            return value if isinstance(value, str) else None
        return None

    def admit_standard_operation(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        descriptor_fingerprint: str,
        session_id: str,
        scope_kind: ModelInputScopeKind,
        turn_id: str,
        tool_call_id: str,
    ) -> McpDispatchAdmissionPermit | None:
        candidate = self._validate_standard_operation(
            tool_name,
            arguments,
            scope_kind=scope_kind,
        )
        if candidate is None:
            return None
        server_id = candidate.server_id
        policy_fp = context_fingerprint(
            "mcp-standard-read-policy:v1",
            (tool_name, server_id, candidate.discovery_snapshot.catalog_semantic_fingerprint),
        )
        runtime_lease = self.slot_lease_by_server.get(server_id)
        if runtime_lease is None:
            raise McpSnapshotStale("MCP runtime lease is unavailable")
        return runtime_lease._slot.admit_dispatch(  # noqa: SLF001
            lease=runtime_lease,
            session_id=session_id,
            scope_kind=scope_kind,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            descriptor_fingerprint=descriptor_fingerprint,
            policy_fingerprint=policy_fp,
            runtime_generation_id=self.runtime_generation_id,
        )

    def _validate_standard_operation(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        scope_kind: ModelInputScopeKind,
    ) -> McpInstallationCandidate | None:
        server_id = self.standard_operation_server_id(tool_name, arguments)
        if server_id is None:
            return None
        visible_server_ids = {
            item.server_id
            for item in self.catalog_for_scope(scope_kind).servers
        }
        if server_id not in visible_server_ids:
            raise ValueError("MCP server is not visible in this scope")
        candidate = self.candidates.get(server_id)
        if candidate is None:
            raise McpSnapshotStale("MCP server is not installed")
        if tool_name == "read_mcp_resource":
            _standard_resource_uri(arguments, candidate.discovery_snapshot)
            return candidate
        raise ValueError("unknown MCP standard operation")

    async def invoke_standard(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        permit: McpDispatchAdmissionPermit | None,
        scope_kind: ModelInputScopeKind,
        cell_cancel_event: asyncio.Event | None = None,
    ) -> McpKnownToolResult:
        from .sdk_facade import MCPError

        if permit is None:
            raise RuntimeError("MCP remote read lost its dispatch permit")
        if permit.scope_kind is not scope_kind:
            permit.release()
            raise McpSnapshotStale("MCP standard operation scope is stale")
        candidate = self._validate_standard_operation(
            tool_name,
            arguments,
            scope_kind=scope_kind,
        )
        if (
            candidate is None
            or permit.runtime_generation_id != self.runtime_generation_id
            or self.slot_lease_by_server.get(candidate.server_id) is not permit.lease
        ):
            permit.release()
            raise McpSnapshotStale("MCP standard operation generation is stale")
        remote_identity, remote_identity_verification = _mcp_standard_operation_identity(
            server_id=candidate.server_id,
            connection_generation=permit.lease.connection_generation,
            tool_name=tool_name,
        )
        permit.mark_attempt_accepted()
        try:
            operation = await permit.acquire_operation(cell_cancel_event=cell_cancel_event)
        except _McpDispatchCancelledBeforeStart:
            permit.release()
            return McpKnownToolResult(
                "CANCELLED_BEFORE_DISPATCH", "Cell 已停止".encode("utf-8"),
                remote_identity, remote_identity_verification,
            )
        except BaseException:
            if permit.state is McpDispatchPermitState.ATTEMPT_ACCEPTED:
                permit.release()
            raise
        async with operation:
            runtime_lease = self.slot_lease_by_server.get(candidate.server_id)
            if runtime_lease is None:
                raise McpSnapshotStale("MCP runtime lease is unavailable")
            client = runtime_lease._slot.client  # noqa: SLF001
            input_owner = McpInputRequiredRoundOwner(
                operation_identity=permit.permit_id,
                connection_generation=runtime_lease.connection_generation,
            )
            try:
                try:
                    async with asyncio.timeout(
                        candidate.standard_read_timeout_ms / 1000
                    ):
                        result = await _invoke_standard_remote_result(
                            client=client,
                            candidate=candidate,
                            tool_name=tool_name,
                            arguments=arguments,
                            input_owner=input_owner,
                        )
                except McpProtocolConformanceError as exc:
                    runtime_lease._slot.report_transport_failure(  # noqa: SLF001
                        "MCP_PROTOCOL_CONFORMANCE_FAILED", retryable=False
                    )
                    return _known_mcp_system_failure(
                        error_code="MCP_RESULT_TYPE_CONFORMANCE_FAILED",
                        remote_identity=remote_identity,
                        remote_identity_verification=remote_identity_verification,
                        message=client.diagnostic_message(exc),
                    )
                except McpTransportOperationError as exc:
                    runtime_lease._slot.report_transport_failure(  # noqa: SLF001
                        "MCP_TRANSPORT_FAILED", retryable=True
                    )
                    if not exc.may_have_reached_server:
                        return _known_mcp_system_failure(
                            error_code="MCP_TRANSPORT_UNWRITTEN",
                            remote_identity=remote_identity,
                            remote_identity_verification=remote_identity_verification,
                        )
                    return _mcp_invocation_failure(
                        error=exc,
                        client=client,
                        remote_identity=remote_identity,
                        remote_identity_verification=remote_identity_verification,
                        effect_kind=McpEffectKind.READ_ONLY,
                    )
                except MCPError as exc:
                    if not _mcp_error_is_transport_failure(exc):
                        return _known_mcp_remote_error(
                            error=exc,
                            client=client,
                            remote_identity=remote_identity,
                            remote_identity_verification=remote_identity_verification,
                        )
                    runtime_lease._slot.report_transport_failure(  # noqa: SLF001
                        "MCP_TRANSPORT_FAILED", retryable=True
                    )
                    return _mcp_invocation_failure(
                        error=exc,
                        client=client,
                        remote_identity=remote_identity,
                        remote_identity_verification=remote_identity_verification,
                        effect_kind=McpEffectKind.READ_ONLY,
                    )
                except McpInputRequiredUnsupported as exc:
                    return _known_mcp_system_failure(
                        error_code="MCP_INPUT_REQUIRED_UNSUPPORTED",
                        remote_identity=remote_identity,
                        remote_identity_verification=remote_identity_verification,
                        message=client.diagnostic_message(exc),
                    )
                except TimeoutError:
                    # Standard resource/prompt operations are frozen read-only
                    # product capabilities.  Their bounded timeout is a known
                    # observation failure, never an unknown external effect.
                    return _known_mcp_system_failure(
                        error_code="MCP_STANDARD_READ_TIMEOUT",
                        remote_identity=remote_identity,
                        remote_identity_verification=remote_identity_verification,
                    )
                except (asyncio.CancelledError, MemoryError):
                    raise
                except Exception as exc:
                    runtime_lease._slot.report_transport_failure(  # noqa: SLF001
                        "MCP_TRANSPORT_FAILED", retryable=True
                    )
                    return _mcp_invocation_failure(
                        error=exc,
                        client=client,
                        remote_identity=remote_identity,
                        remote_identity_verification=remote_identity_verification,
                        effect_kind=McpEffectKind.READ_ONLY,
                    )
            finally:
                input_owner.close()
            try:
                payload = result.model_dump(
                    by_alias=True, mode="json", exclude_none=True
                )
                payload.pop("_meta", None)
                body = canonical_json_bytes(
                    {"trust": "UNTRUSTED_OBSERVATION", "result": payload}
                )
                if len(body) > MAXIMUM_MCP_REMOTE_BODY_BYTES:
                    raise RuntimeError("MCP_REMOTE_BODY_BOUND_EXCEEDED")
            except MemoryError:
                raise
            except Exception as exc:
                # resources/read and prompts/get already returned an exact
                # response.  A carrier/rendering bound failure is known and
                # must not be rewritten into physical ambiguity or replayed.
                return _known_mcp_system_failure(
                    error_code="MCP_RESULT_LOWERING_FAILED",
                    remote_identity=remote_identity,
                    remote_identity_verification=remote_identity_verification,
                    message=client.diagnostic_message(exc),
                )
            return McpKnownToolResult(
                state="SUCCESS",
                content=body,
                remote_identity=remote_identity,
                remote_identity_verification=remote_identity_verification,
            )



def _aggregate_provider_projection(
    candidates: tuple[McpInstallationCandidate, ...],
    scope: ModelInputScopeKind,
) -> tuple[
    tuple[McpToolSemanticFact, ...],
    tuple[McpProviderNameCollisionFact, ...],
]:
    visible_semantics = tuple(
        semantic
        for candidate in candidates
        for semantic in candidate.discovery_snapshot.tools
        if (
            semantic.root_visible
            if scope is ModelInputScopeKind.ROOT
            else semantic.subagent_visible
        )
    )
    return _provider_projection_from_semantics(visible_semantics)


def _provider_projection_from_semantics(
    semantics: tuple[McpToolSemanticFact, ...],
) -> tuple[
    tuple[McpToolSemanticFact, ...],
    tuple[McpProviderNameCollisionFact, ...],
]:
    groups: dict[str, list[McpToolSemanticFact]] = {}
    for semantic in semantics:
        groups.setdefault(semantic.provider_tool_name, []).append(semantic)
    survivors: list[McpToolSemanticFact] = []
    collisions: list[McpProviderNameCollisionFact] = []
    for provider_name in sorted(groups):
        members = groups[provider_name]
        if len(members) == 1:
            survivors.append(members[0])
            continue
        collisions.append(
            McpProviderNameCollisionFact(
                provider_name=provider_name,
                members=tuple(
                    sorted(
                        (
                            McpProviderNameCollisionMember(
                                server_id=item.server_id,
                                remote_tool_name=item.remote_tool_name,
                                discovered_tool_identity=(
                                    item.descriptor_fingerprint
                                ),
                            )
                            for item in members
                        ),
                        key=lambda item: (
                            item.server_id,
                            item.remote_tool_name,
                            item.discovered_tool_identity,
                        ),
                    )
                ),
            )
        )
    return tuple(survivors), tuple(collisions)


def _mcp_config_source_kind(config: McpServerConfig) -> str:
    source = config.runtime_source
    if isinstance(source, LocalConfiguredMcpRuntimeSource):
        return source.source_kind.value
    if isinstance(source, ManagedPackageMcpRuntimeSource):
        return source.kind
    raise TypeError("MCP runtime source identity union is open")


def _candidate_exact_joins_config(
    candidate: McpInstallationCandidate,
    config: McpServerConfig,
    *,
    supervisor_epoch: str,
) -> bool:
    return (
        candidate.server_id == config.server_id
        and candidate.expected_supervisor_epoch == supervisor_epoch
        and candidate.expected_semantic_config_fingerprint
        == config.semantic_config_fingerprint
        and candidate.expected_runtime_config_fingerprint
        == config.runtime_config_fingerprint
        and candidate.expected_resolved_config_identity
        == config.resolved_config_identity
    )


DEFAULT_MCP_CONNECT_ATTEMPT_TIMEOUT_SECONDS = 120.0


class McpHostSupervisor:
    """The only owner allowed to connect, fence, and close MCP slots."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        configs: tuple[McpServerConfig, ...],
        credential_boundary: ProcessCredentialBoundary,
        client_factory: type[BoundedMcpSdkClient] | None = None,
        connect_attempt_timeout_seconds: float = DEFAULT_MCP_CONNECT_ATTEMPT_TIMEOUT_SECONDS,
        configuration_change: asyncio.Event | None = None,
        adopt_configuration: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        if len(configs) > MAXIMUM_CONFIGURED_MCP_SERVERS:
            raise ValueError("too many configured MCP servers")
        self.workspace_root = workspace_root.resolve()
        self.configs = tuple(sorted(configs, key=lambda item: item.server_id))
        self._config_by_id = {item.server_id: item for item in self.configs}
        self._client_factory = client_factory
        self._credential_boundary = credential_boundary
        if (configuration_change is None) != (adopt_configuration is None):
            raise ValueError("MCP configuration observation requires its adoption owner")
        self._adoption_requested = configuration_change
        self._adopt_configuration = adopt_configuration
        if connect_attempt_timeout_seconds <= 0:
            raise ValueError("MCP connection watchdog must be positive")
        self._connect_attempt_timeout_seconds = connect_attempt_timeout_seconds
        self._epoch = random_id("epoch")
        self._lock = RLock()
        self._state = {
            item.server_id: (
                McpServerState.CONNECTING if item.enabled else McpServerState.DISABLED
            )
            for item in self.configs
        }
        self._state_config_identity = {
            item.server_id: item.resolved_config_identity for item in self.configs
        }
        self._failure: dict[str, str] = {}
        self._attempt_generation = {item.server_id: 0 for item in self.configs}
        self._refresh_generation = {item.server_id: 0 for item in self.configs}
        self._slots: dict[str, McpConnectionSlot] = {}
        self._all_slots: set[McpConnectionSlot] = set()
        self._slot_close_tasks: set[asyncio.Task[None]] = set()
        # Current-attempt lookup may be replaced while an old attempt still
        # owns physical cleanup. Keep that owner until its task has joined.
        self._connection_tasks: set[asyncio.Task[_McpConnectOutcome]] = set()
        self._pending: dict[str, McpInstallationCandidate] = {}
        self._installed: dict[str, McpInstallationCandidate] = {}
        self._tasks: dict[str, asyncio.Task[_McpConnectOutcome]] = {}
        self._refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._retry_counts = {item.server_id: 0 for item in self.configs}
        self._catalog_revision = 0
        self._runtime_generation = 0
        self._surface_rebuild_required = False
        # One normalized discovery candidate may retain up to 32 MiB while the
        # wire carrier and canonicalized item are both alive.  Serializing
        # discovery keeps that physical overlap comfortably inside the sealed
        # 128 MiB Host budget without weakening the product catalog bound.
        self._discovery_reservations = asyncio.Semaphore(1)
        self._host_operation_lane = asyncio.Semaphore(MAXIMUM_MCP_HOST_IN_FLIGHT)
        self._periodic_tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._capability_owner_authenticity = object()
        self._configuration_changed = asyncio.Event()
        # Only current execution observers live here; outcomes remain owned by
        # the existing connection tasks, including a failed task being retried.
        self._readiness_waits: dict[
            asyncio.Task, tuple[tuple[McpServerConfig, asyncio.Task[_McpConnectOutcome]], ...]
        ] = {}

    @property
    def owner_epoch(self) -> str:
        return self._epoch

    @property
    def physical_close_complete(self) -> bool:
        with self._lock:
            return (self._close_task is not None and self._close_task.done()
                    and not self._all_slots
                    and all(task.done() for task in self._connection_tasks)
                    and all(task.done() for task in self._slot_close_tasks))

    @property
    def local_children_stopped(self) -> bool:
        with self._lock:
            # Import/HTTP cleanup tasks own no local children. After the last
            # import await, _connect checks this same fence and synchronously
            # publishes its client slot before any possible stdio launch.
            return (self._closed
                    and all(slot.client.local_children_stopped for slot in self._all_slots))

    async def start(self) -> None:
        """Schedule enabled peers in config order without waiting for network IO."""
        enabled = tuple(item for item in self.configs if item.enabled)
        for config in enabled:
            self._start_connect(config.server_id)
            if config.catalog_refresh_interval_ms is not None:
                self._periodic_tasks[config.server_id] = asyncio.create_task(
                    self._periodic_refresh(config.server_id),
                    name=f"mcp-periodic-refresh:{config.server_id}",
                )

    async def wait_initial_readiness(self, *, conversation_scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT) -> None:
        """Observe the captured attempts once, never their automatic successors.

        No input snapshot or surface borrow may be held by the caller. A
        cancelled observer leaves connection/cleanup ownership with supervisor.
        Config replacement releases that observer without waiting for old IO.
        """
        observer = asyncio.current_task()
        assert observer is not None
        with self._lock:
            if self._closed:
                raise asyncio.CancelledError
            captured = tuple(
                (config, self._tasks[config.server_id])
                for config in self.configs
                if config.enabled
                and config.server_id in self._tasks
                and not self._installed_semantic_surface_retained_locked(config.server_id)
                and (conversation_scope_kind is ModelInputScopeKind.ROOT
                     or config.scope_policy is McpScopePolicy.ROOT_AND_SUBAGENTS)
            )
            self._readiness_waits[observer] = captured
        try:
            while True:
                if self._adoption_requested is not None and self._adoption_requested.is_set():
                    self._adoption_requested.clear()
                    assert self._adopt_configuration is not None
                    await self._adopt_configuration()
                    if self._adoption_requested.is_set():
                        continue
                with self._lock:
                    if self._closed:
                        raise asyncio.CancelledError
                    current = tuple(
                        (config, task) for config, task in captured
                        if (actual := self._config_by_id.get(config.server_id)) is not None
                        and actual.enabled
                        and actual.resolved_config_identity == config.resolved_config_identity
                    )
                    changed = self._configuration_changed
                pending = set()
                for _config, task in current:
                    if task.done():
                        # Expected connect/discovery failures are typed outcomes.
                        # Unexpected cleanup/owner exceptions must still escape.
                        task.result()
                    else:
                        pending.add(task)
                if not pending:
                    return
                configuration_wait = asyncio.create_task(changed.wait())
                adoption_wait = (None if self._adoption_requested is None else
                                 asyncio.create_task(self._adoption_requested.wait()))
                try:
                    waits = (*pending, configuration_wait) if adoption_wait is None else (*pending, configuration_wait, adoption_wait)
                    await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    configuration_wait.cancel()
                    observers = [configuration_wait]
                    if adoption_wait is not None:
                        adoption_wait.cancel()
                        observers.append(adoption_wait)
                    await asyncio.gather(*observers, return_exceptions=True)
        finally:
            with self._lock:
                self._readiness_waits.pop(observer, None)

    def readiness_progress(self) -> dict[str, object] | None:
        """Disposable current-execution progress; reads never start/wait work."""
        with self._lock:
            if not self._readiness_waits:
                return None
            servers = {}
            for captured in self._readiness_waits.values():
                for config, task in captured:
                    actual = self._config_by_id.get(config.server_id)
                    if (actual is None or not actual.enabled
                            or actual.resolved_config_identity != config.resolved_config_identity):
                        continue
                    if task.done() and not task.cancelled() and task.exception() is None:
                        outcome = task.result()
                        state, failure = outcome.state, outcome.failure
                    else:
                        state, failure = self._live_state_locked(config, None)
                    servers[config.server_id] = {"server_id": config.server_id,
                        "display_name": config.display_name, "state": state.value,
                        "failure": failure}
            values = list(servers.values())
            return {"servers": values, "waiting": any(item["state"] in {
                McpServerState.CONNECTING.value, McpServerState.DISCOVERING.value,
            } for item in values)}

    def reload_configs(
        self, configs: tuple[McpServerConfig, ...]
    ) -> frozenset[str]:
        """Install one typed config epoch; physical work starts outside the lock."""

        if len(configs) > MAXIMUM_CONFIGURED_MCP_SERVERS:
            raise ValueError("too many configured MCP servers")
        ordered = tuple(sorted(configs, key=lambda item: item.server_id))
        incoming = {item.server_id: item for item in ordered}
        if len(incoming) != len(ordered):
            raise ValueError("MCP server ids are not unique")
        reconnect: list[str] = []
        retire_slots: list[McpConnectionSlot] = []
        release_candidates: list[McpInstallationCandidate] = []
        cancel_tasks: list[asyncio.Task[None] | asyncio.Task[_McpConnectOutcome]] = []
        close_config_anchors: list[object] = []
        with self._lock:
            if self._closed:
                raise RuntimeError("MCP supervisor is closed")
            self._configuration_changed.set()
            self._configuration_changed = asyncio.Event()
            old = self._config_by_id
            changed_ids = {
                server_id
                for server_id in set(old) | set(incoming)
                if server_id not in old
                or server_id not in incoming
                or old[server_id].resolved_config_identity
                != incoming[server_id].resolved_config_identity
                or old[server_id].enabled != incoming[server_id].enabled
            }
            updated = dict(incoming)
            for server_id in set(old) & set(incoming) - changed_ids:
                updated[server_id] = old[server_id]
                anchor = incoming[server_id].physical_lifetime_anchor
                if anchor is not None:
                    close_config_anchors.append(anchor)
            for server_id in changed_ids:
                old_config = old.get(server_id)
                if (
                    old_config is not None
                    and old_config.physical_lifetime_anchor is not None
                ):
                    close_config_anchors.append(
                        old_config.physical_lifetime_anchor
                    )
            self.configs = tuple(updated[key] for key in sorted(updated))
            self._config_by_id = updated
            for server_id in changed_ids:
                old_config = old.get(server_id)
                new_config = updated.get(server_id)
                retain_semantic_surface = (
                    old_config is not None
                    and new_config is not None
                    and old_config.enabled
                    and new_config.enabled
                    and old_config.semantic_config_fingerprint
                    == new_config.semantic_config_fingerprint
                )
                if not retain_semantic_surface:
                    self._surface_rebuild_required = True
                self._attempt_generation[server_id] = (
                    self._attempt_generation.get(server_id, 0) + 1
                )
                self._refresh_generation[server_id] = (
                    self._refresh_generation.get(server_id, 0) + 1
                )
                for registry in (
                    self._tasks,
                    self._refresh_tasks,
                    self._retry_tasks,
                    self._periodic_tasks,
                ):
                    task = registry.pop(server_id, None)
                    if task is not None and not task.done():
                        cancel_tasks.append(task)
                pending = self._pending.pop(server_id, None)
                installed = (
                    None
                    if retain_semantic_surface
                    else self._installed.pop(server_id, None)
                )
                if pending is not None:
                    release_candidates.append(pending)
                if installed is not None:
                    release_candidates.append(installed)
                slot = self._slots.pop(server_id, None)
                if slot is not None:
                    # Existing admitted operations may drain, but no old
                    # generation may admit a fresh dispatch after config
                    # replacement/removal.  For a runtime-only change the old
                    # semantic candidate remains installed until its exact
                    # replacement reaches a safe point, preserving the Round
                    # 3.1 provider surface without keeping old physical access.
                    slot.mark_dirty()
                    retire_slots.append(slot)
                config = updated.get(server_id)
                if config is None:
                    self._state.pop(server_id, None)
                    self._state_config_identity.pop(server_id, None)
                    self._failure.pop(server_id, None)
                    self._retry_counts.pop(server_id, None)
                    self._refresh_generation.pop(server_id, None)
                else:
                    self._retry_counts[server_id] = 0
                    if not retain_semantic_surface:
                        self._failure.pop(server_id, None)
                        self._state[server_id] = (
                            McpServerState.CONNECTING
                            if config.enabled
                            else McpServerState.DISABLED
                        )
                        self._state_config_identity[server_id] = (
                            config.resolved_config_identity
                        )
                    if config.enabled:
                        reconnect.append(server_id)
            for server_id, config in updated.items():
                if server_id in old:
                    continue
                self._attempt_generation.setdefault(server_id, 0)
                self._refresh_generation.setdefault(server_id, 0)
                self._retry_counts.setdefault(server_id, 0)
                self._state[server_id] = (
                    McpServerState.CONNECTING
                    if config.enabled
                    else McpServerState.DISABLED
                )
                self._state_config_identity[server_id] = (
                    config.resolved_config_identity
                )
                if config.enabled and server_id not in reconnect:
                    reconnect.append(server_id)
        for candidate in release_candidates:
            candidate.slot_lease.release()
        for anchor in close_config_anchors:
            _close_physical_lifetime_anchor(anchor)
        for task in cancel_tasks:
            task.cancel()
        for slot in retire_slots:
            task = asyncio.create_task(
                self._close_retired_slot(slot),
                name=f"mcp-slot-retire:{slot.slot_id}",
            )
            self._slot_close_tasks.add(task)
            task.add_done_callback(self._slot_close_tasks.discard)
        for server_id in reconnect:
            self._start_connect(server_id)
            config = updated[server_id]
            if config.catalog_refresh_interval_ms is not None:
                self._periodic_tasks[server_id] = asyncio.create_task(
                    self._periodic_refresh(server_id),
                    name=f"mcp-periodic-refresh:{server_id}",
                )
        return frozenset(changed_ids)

    def _start_connect(self, server_id: str) -> None:
        with self._lock:
            if self._closed:
                return
            config = self._config_by_id.get(server_id)
            if config is None or not config.enabled:
                return
            previous = self._tasks.get(server_id)
            if previous is not None and not previous.done():
                return
            self._attempt_generation[server_id] += 1
            generation = self._attempt_generation[server_id]
            refresh_generation = self._refresh_generation[server_id]
            attempt_anchor = _duplicate_physical_lifetime_anchor(
                config.physical_lifetime_anchor
            )
            # A same-semantic replacement attempt is physical state only.  Its
            # installed catalog remains provider-visible while execution is
            # independently fenced, so CONNECTING would create a false catalog
            # transition on every runtime-only reconnect.
            if not self._installed_semantic_surface_retained_locked(server_id):
                self._set_catalog_state_locked(
                    server_id, McpServerState.CONNECTING
                )
            task = asyncio.create_task(
                self._connect(
                    server_id,
                    generation,
                    refresh_generation,
                    config,
                    attempt_anchor,
                ),
                name=f"mcp-connect:{server_id}:{generation}",
            )
            self._tasks[server_id] = task
            self._connection_tasks.add(task)
            task.add_done_callback(self._connection_tasks.discard)
            task.add_done_callback(self._consume_connection_outcome)

    @staticmethod
    def _consume_connection_outcome(task: asyncio.Task[_McpConnectOutcome]) -> None:
        if task.cancelled():
            return
        with suppress(BaseException):
            task.result()

    def _installed_available_locked(self, server_id: str) -> bool:
        candidate = self._installed.get(server_id)
        return candidate is not None and candidate.slot_lease._slot.lease_is_current(  # noqa: SLF001
            candidate.slot_lease
        )

    def _installed_semantic_surface_retained_locked(self, server_id: str) -> bool:
        """Return whether an installed catalog remains valid during physical rebind."""

        candidate = self._installed.get(server_id)
        config = self._config_by_id.get(server_id)
        return (
            candidate is not None
            and config is not None
            and config.enabled
            and candidate.expected_semantic_config_fingerprint
            == config.semantic_config_fingerprint
        )

    def _set_catalog_state_locked(
        self,
        server_id: str,
        state: McpServerState,
        *,
        failure: str | None = None,
        resolved_config_identity: str | None = None,
    ) -> None:
        """Request a catalog-only successor for a visible state transition."""

        previous = (self._state.get(server_id), self._failure.get(server_id))
        if (failure is None
                and state in {McpServerState.CONNECTING, McpServerState.DISCOVERING}):
            failure = self._failure.get(server_id)
        if resolved_config_identity is None:
            config = self._config_by_id.get(server_id)
            if config is None:
                raise RuntimeError("MCP state has no current config identity")
            resolved_config_identity = config.resolved_config_identity
        self._state[server_id] = state
        self._state_config_identity[server_id] = resolved_config_identity
        if failure is None:
            self._failure.pop(server_id, None)
        else:
            self._failure[server_id] = failure
        if self._runtime_generation > 0 and previous != (state, failure):
            self._surface_rebuild_required = True

    async def _connect(
        self,
        server_id: str,
        attempt_generation: int,
        refresh_generation: int,
        config: McpServerConfig,
        attempt_anchor: object | None,
    ) -> _McpConnectOutcome:
        box: dict[str, McpConnectionSlot] = {}

        async def notification(method: str) -> None:
            slot = box.get("slot")
            if slot is None:
                return
            if method == "pulsara/sessionful_transport":
                if slot.concurrency_kind is McpPhysicalConcurrencyKind.BOUNDED_STATELESS_HTTP:
                    slot.report_transport_failure(
                        "MCP_PROTOCOL_CONFORMANCE_FAILED", retryable=False
                    )
                return
            if method == "pulsara/protocol_conformance_failure":
                slot.report_transport_failure(
                    "MCP_PROTOCOL_CONFORMANCE_FAILED", retryable=False
                )
                return
            if method == "pulsara/transport_failure":
                slot.report_transport_failure(
                    "MCP_TRANSPORT_FAILED", retryable=True
                )
                return
            slot.mark_dirty()
            with self._lock:
                if self._closed:
                    return
                current = self._slots.get(server_id)
                if (
                    current is not slot
                    or self._attempt_generation.get(server_id)
                    != attempt_generation
                ):
                    return
                self._refresh_generation[server_id] += 1
                refresh = self._refresh_tasks.get(server_id)
                if refresh is None or refresh.done():
                    self._refresh_tasks[server_id] = asyncio.create_task(
                        self._refresh_after_dirty(server_id),
                        name=f"mcp-reconcile:{server_id}",
                    )

        client: BoundedMcpSdkClient | None = None
        slot: McpConnectionSlot | None = None
        candidate_lease: McpSlotLease | None = None
        try:
            superseded_pending: McpInstallationCandidate | None = None
            # Waiting for the shared memory reservation is scheduling, not a
            # remote connection attempt. Start its budget only after admission;
            # keep the reservation through parsing and candidate publication.
            async with self._discovery_reservations:
                # Reserve in stable config order before first-use SDK imports.
                factory = self._client_factory
                if factory is None:
                    factory = await _prepare_sdk_client_factory()
                with self._lock:
                    if (self._closed
                            or self._attempt_generation.get(server_id) != attempt_generation
                            or self._refresh_generation.get(server_id) != refresh_generation):
                        raise RuntimeError("stale MCP connection attempt")
                    # SDK construction has no IO. Keep the fence check and
                    # slot publication synchronous, before open can launch a
                    # local process or yield to another lifecycle observer.
                    client = factory(
                        config,
                        workspace_root=self.workspace_root,
                        notification_callback=notification,
                        credential_boundary=self._credential_boundary,
                    )
                    # Start serial until the HTTP handshake proves whether
                    # an asserted stateless peer installed session state.
                    slot = McpConnectionSlot(
                        server_id=server_id,
                        supervisor_epoch=self._epoch,
                        connection_generation=attempt_generation,
                        runtime_config_fingerprint=config.runtime_config_fingerprint,
                        client=client,
                        concurrency_kind=McpPhysicalConcurrencyKind.SERIAL_SESSION,
                        maximum_in_flight=1,
                        host_lane=self._host_operation_lane,
                        failure_reporter=self._report_slot_failure,
                        physical_lifetime_anchor=attempt_anchor,
                    )
                    attempt_anchor = None
                    box["slot"] = slot
                    self._all_slots.add(slot)
                async with asyncio.timeout(self._connect_attempt_timeout_seconds):
                    await client.open()
                    with self._lock:
                        if (
                            self._closed
                            or self._attempt_generation[server_id]
                            != attempt_generation
                            or self._refresh_generation[server_id]
                            != refresh_generation
                        ):
                            raise RuntimeError("stale MCP connection attempt")
                        if not self._installed_semantic_surface_retained_locked(
                            server_id
                        ):
                            self._set_catalog_state_locked(
                                server_id, McpServerState.DISCOVERING
                            )
                    snapshot, policies = await discover_mcp_catalog(client, config)
                    if client.supports_bounded_stateless_parallelism:
                        mode = McpPhysicalConcurrencyKind.BOUNDED_STATELESS_HTTP
                        maximum = config.stateless_http_max_in_flight
                        slot.configure_physical_concurrency(
                            concurrency_kind=mode,
                            maximum_in_flight=maximum,
                        )
                    candidate_lease = slot.issue_lease(0)
                    candidate = _candidate(
                        config=config,
                        epoch=self._epoch,
                        attempt_generation=attempt_generation,
                        lease=candidate_lease,
                        snapshot=snapshot,
                        policies=policies,
                    )
                    with self._lock:
                        if (
                            self._closed
                            or self._attempt_generation[server_id]
                            != attempt_generation
                            or self._refresh_generation[server_id]
                            != refresh_generation
                            or self._config_by_id[
                                server_id
                            ].resolved_config_identity
                            != config.resolved_config_identity
                        ):
                            raise RuntimeError("stale MCP candidate")
                        effective_candidates = {
                            **self._installed,
                            **self._pending,
                            server_id: candidate,
                        }
                        if sum(
                            item.normalized_physical_bytes
                            for item in effective_candidates.values()
                        ) > (
                            DEFAULT_MCP_WIRE_BOUNDS.maximum_discovery_candidate_bytes_per_host
                        ):
                            raise ValueError(
                                "MCP Host discovery candidate byte bound exceeded"
                            )
                        superseded_pending = self._pending.get(server_id)
                        self._slots[server_id] = slot
                        self._pending[server_id] = candidate
                        self._set_catalog_state_locked(
                            server_id, McpServerState.READY
                        )
                        self._retry_counts[server_id] = 0
            if superseded_pending is not None:
                superseded_pending.slot_lease.release()
                superseded_slot = superseded_pending.slot_lease._slot  # noqa: SLF001
                if superseded_slot is not slot:
                    superseded_slot.begin_close()
                    task = asyncio.create_task(
                        self._close_retired_slot(superseded_slot),
                        name=f"mcp-slot-retire:{superseded_slot.slot_id}",
                    )
                    self._slot_close_tasks.add(task)
                    task.add_done_callback(self._slot_close_tasks.discard)
            return _McpConnectOutcome(McpServerState.READY)
        except BaseException as exc:
            if candidate_lease is not None:
                candidate_lease.release()
            if slot is not None:
                slot.begin_close()
            if client is not None:
                close = asyncio.create_task(
                    client.aclose(), name=f"mcp-connect-close:{server_id}:{attempt_generation}"
                )
                while not close.done():
                    try:
                        await asyncio.shield(close)
                    except asyncio.CancelledError:
                        # Replacement or Host close cannot detach the exact
                        # physical cleanup, even if cancellation arrives again.
                        continue
                close.result()
            if slot is not None:
                slot.close_physical_lifetime()
                with self._lock:
                    self._all_slots.discard(slot)
            with self._lock:
                if (
                    not self._closed
                    and self._attempt_generation[server_id] == attempt_generation
                ):
                    semantic_surface_retained = (
                        self._installed_semantic_surface_retained_locked(server_id)
                    )
                    if semantic_surface_retained:
                        # A failed replacement must not withdraw the retained
                        # semantic catalog or publish an operational failure as
                        # catalog semantics.  Physical execution remains fenced.
                        retained_candidate = self._installed.get(server_id)
                        if retained_candidate is None:
                            raise RuntimeError(
                                "retained MCP surface lost its config identity"
                            )
                        self._set_catalog_state_locked(
                            server_id,
                            McpServerState.READY,
                            resolved_config_identity=(
                                retained_candidate.expected_resolved_config_identity
                            ),
                        )
                    else:
                        self._set_catalog_state_locked(
                            server_id,
                            (
                                McpServerState.FAILED_TERMINAL
                                if isinstance(exc, ValueError)
                                else McpServerState.FAILED_RETRYABLE
                            ),
                            failure=type(exc).__name__,
                        )
                    if not isinstance(exc, ValueError):
                        self._schedule_retry_locked(server_id)
            return _McpConnectOutcome(
                McpServerState.FAILED_TERMINAL
                if isinstance(exc, ValueError)
                else McpServerState.FAILED_RETRYABLE,
                type(exc).__name__,
            )
        finally:
            if attempt_anchor is not None:
                _close_physical_lifetime_anchor(attempt_anchor)

    def _report_slot_failure(
        self,
        slot: McpConnectionSlot,
        category: str,
        retryable: bool,
    ) -> None:
        release_candidates: list[McpInstallationCandidate] = []
        should_close = False
        with self._lock:
            if self._closed:
                return
            server_id = slot.server_id
            pending = self._pending.get(server_id)
            installed = self._installed.get(server_id)
            pending_is_failed_slot = (
                pending is not None and pending.slot_lease._slot is slot  # noqa: SLF001
            )
            installed_is_failed_slot = (
                installed is not None and installed.slot_lease._slot is slot  # noqa: SLF001
            )
            current_is_failed_slot = self._slots.get(server_id) is slot
            if not (
                pending_is_failed_slot
                or installed_is_failed_slot
                or current_is_failed_slot
            ):
                # A superseded/retired slot owns only its already-scheduled
                # physical close.  It must never mutate another generation's
                # public state or candidate ownership.
                return
            failed_config_identity: str | None = None
            if pending_is_failed_slot:
                assert pending is not None
                failed_config_identity = pending.expected_resolved_config_identity
            elif installed_is_failed_slot:
                assert installed is not None
                failed_config_identity = installed.expected_resolved_config_identity
            if pending_is_failed_slot:
                assert pending is not None
                self._pending.pop(server_id, None)
                release_candidates.append(pending)
            if installed_is_failed_slot:
                assert installed is not None
                self._installed.pop(server_id, None)
                release_candidates.append(installed)
                self._surface_rebuild_required = True
            if current_is_failed_slot:
                replacement = self._installed.get(server_id)
                if replacement is None:
                    replacement = self._pending.get(server_id)
                if replacement is None:
                    self._slots.pop(server_id, None)
                else:
                    self._slots[server_id] = replacement.slot_lease._slot  # noqa: SLF001
            replacement_available = self._installed_available_locked(server_id)
            installed_replacement = self._installed.get(server_id)
            pending_replacement = self._pending.get(server_id)
            if replacement_available or pending_replacement is not None:
                # The failed exact slot is operational history.  A healthy
                # installed surface (or ready pending replacement) remains the
                # semantic catalog truth.
                self._set_catalog_state_locked(
                    server_id,
                    McpServerState.READY,
                    resolved_config_identity=(
                        pending_replacement.expected_resolved_config_identity
                        if pending_replacement is not None
                        else (
                            installed_replacement.expected_resolved_config_identity
                            if installed_replacement is not None
                            else None
                        )
                    ),
                )
            else:
                self._set_catalog_state_locked(
                    server_id,
                    (
                        McpServerState.FAILED_RETRYABLE
                        if retryable
                        else McpServerState.FAILED_TERMINAL
                    ),
                    failure=category,
                    resolved_config_identity=failed_config_identity,
                )
            if retryable:
                self._schedule_retry_locked(server_id)
            should_close = True
        for candidate in release_candidates:
            candidate.slot_lease.release()
        if should_close:
            task = asyncio.create_task(
                self._close_retired_slot(slot),
                name=f"mcp-slot-terminal:{slot.slot_id}",
            )
            self._slot_close_tasks.add(task)
            task.add_done_callback(self._slot_close_tasks.discard)

    def _schedule_retry_locked(self, server_id: str) -> None:
        current = self._retry_tasks.get(server_id)
        if current is not None and not current.done():
            return
        count = self._retry_counts[server_id]
        self._retry_counts[server_id] = min(count + 1, 16)
        base = min(60.0, float(2**min(count, 6)))
        delay = base * random.uniform(0.8, 1.2)
        self._retry_tasks[server_id] = asyncio.create_task(
            self._retry_after(server_id, delay),
            name=f"mcp-connect-retry:{server_id}:{count + 1}",
        )

    async def _retry_after(self, server_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        with self._lock:
            if (
                self._closed
                or server_id not in self._config_by_id
                or not self._config_by_id[server_id].enabled
            ):
                return
        self._start_connect(server_id)

    async def _refresh_after_dirty(self, server_id: str) -> None:
        # Coalesce notifications, then relist on the same physical owner.
        await asyncio.sleep(0.01)
        while True:
            with self._lock:
                slot = self._slots.get(server_id)
                config = self._config_by_id.get(server_id)
                if (self._closed or slot is None or config is None or not config.enabled
                        or slot.dispatch_fence_state is McpDispatchFenceState.CLOSED):
                    return
                attempt = self._attempt_generation[server_id]
                refresh = self._refresh_generation[server_id]
                dirty = slot.dirty_generation
            lease = None
            try:
                async with slot.catalog_operation(
                    discovery_reservation=self._discovery_reservations
                ):
                    async with asyncio.timeout(self._connect_attempt_timeout_seconds):
                        snapshot, policies = await discover_mcp_catalog(slot.client, config)
                    with self._lock:
                        if (self._closed or self._slots.get(server_id) is not slot
                                or self._config_by_id.get(server_id) is not config
                                or self._attempt_generation[server_id] != attempt
                                or slot.dispatch_fence_state is McpDispatchFenceState.CLOSED):
                            return
                        if (self._refresh_generation[server_id] != refresh
                                or slot.dirty_generation != dirty):
                            continue
                        lease = slot.prepare_refresh_lease(dirty)
                        candidate = _candidate(
                            config=config, epoch=self._epoch,
                            attempt_generation=attempt, lease=lease,
                            snapshot=snapshot, policies=policies,
                        )
                        effective = {**self._installed, **self._pending, server_id: candidate}
                        if sum(item.normalized_physical_bytes for item in effective.values()) > (
                            DEFAULT_MCP_WIRE_BOUNDS.maximum_discovery_candidate_bytes_per_host
                        ):
                            raise ValueError("MCP Host discovery candidate byte bound exceeded")
                        previous = self._pending.get(server_id)
                        slot.complete_refresh(lease)
                        self._pending[server_id] = candidate
                        lease = None  # Candidate now owns this lease.
                        self._set_catalog_state_locked(server_id, McpServerState.READY)
                    if previous is not None:
                        previous.slot_lease.release()
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                slot.report_transport_failure(type(exc).__name__, retryable=not isinstance(exc, ValueError))
                return
            finally:
                if lease is not None:
                    lease.release()

    async def _periodic_refresh(self, server_id: str) -> None:
        config = self._config_by_id[server_id]
        assert config.catalog_refresh_interval_ms is not None
        interval = config.catalog_refresh_interval_ms / 1000
        while True:
            await asyncio.sleep(interval)
            with self._lock:
                if (self._closed or server_id not in self._config_by_id
                        or not self._config_by_id[server_id].enabled):
                    return
                slot = self._slots.get(server_id)
                if slot is None or slot.dispatch_fence_state is McpDispatchFenceState.CLOSED:
                    continue  # Initial connect/failure retry has its own owner.
                slot.mark_dirty()
                self._refresh_generation[server_id] += 1
                refresh = self._refresh_tasks.get(server_id)
                if refresh is None or refresh.done():
                    task = asyncio.create_task(self._refresh_after_dirty(server_id),
                                               name=f"mcp-reconcile:{server_id}")
                    self._refresh_tasks[server_id] = task

    async def _close_retired_slot(self, slot: McpConnectionSlot) -> None:
        while (
            slot.active_slot_lease_count
            or slot.active_dispatch_count
            or slot.active_operation_count
        ):
            await asyncio.sleep(0.01)
        slot.begin_close()
        closed = False
        try:
            await slot.client.aclose()
            closed = True
        finally:
            if closed or slot.client.physical_close_complete:
                slot.close_physical_lifetime()
                with self._lock:
                    self._all_slots.discard(slot)
                    if self._slots.get(slot.server_id) is slot:
                        self._slots.pop(slot.server_id, None)

    def install_pending_at_safe_point(self) -> McpInstalledRuntimeGeneration | None:
        """Atomically install all current candidates; performs no physical I/O."""

        replaced_candidates: list[McpInstallationCandidate] = []
        with self._lock:
            if self._closed:
                raise RuntimeError("MCP supervisor is closed")
            accepted_pending: dict[str, McpInstallationCandidate] = {}
            next_installed = dict(self._installed)
            for server_id, candidate in tuple(self._pending.items()):
                config = self._config_by_id.get(server_id)
                if (
                    config is None
                    or not _candidate_exact_joins_config(
                        candidate,
                        config,
                        supervisor_epoch=self._epoch,
                    )
                    or not candidate.slot_lease._slot.lease_is_current(  # noqa: SLF001
                        candidate.slot_lease
                    )
                ):
                    # A dirty candidate stays owned until its full relist
                    # supersedes it.  Publishing it would reopen dispatch;
                    # dropping it here would detach the old physical slot.
                    continue
                accepted_pending[server_id] = candidate
                next_installed[server_id] = candidate
            changed = bool(accepted_pending) or self._surface_rebuild_required
            if not changed and self._runtime_generation > 0:
                return None
            candidates = tuple(
                next_installed[key] for key in sorted(next_installed)
            )
            if any(
                not candidate.slot_lease._slot.lease_is_current(candidate.slot_lease)
                for candidate in candidates
            ):
                # Another server may still be draining its pre-fence calls.
                # Keep all pending catalogs owned until a complete runtime can
                # be published; never reopen or rewrite that old lease here.
                return None
            root, root_collisions = _aggregate_provider_projection(
                candidates, ModelInputScopeKind.ROOT
            )
            child, child_collisions = _aggregate_provider_projection(
                candidates, ModelInputScopeKind.SUBAGENT
            )
            execution_semantics = tuple(
                sorted(
                    {
                        (item.server_id, item.remote_tool_name): item
                        for item in (*root, *child)
                    }.values(),
                    key=lambda item: item.provider_tool_name,
                )
            )
            lease_by_server: dict[str, McpSlotLease] = {}
            try:
                for candidate in candidates:
                    lease_by_server[candidate.server_id] = (
                        candidate.slot_lease._slot.issue_lease(  # noqa: SLF001
                            candidate.slot_lease.admitted_discovery_generation
                        )
                    )
            except BaseException:
                for lease in lease_by_server.values():
                    lease.release()
                raise
            for server_id, candidate in accepted_pending.items():
                previous_candidate = self._installed.get(server_id)
                self._installed[server_id] = candidate
                if self._pending.get(server_id) is candidate:
                    self._pending.pop(server_id, None)
                if previous_candidate is not None:
                    replaced_candidates.append(previous_candidate)
            self._surface_rebuild_required = False
            if changed:
                self._catalog_revision += 1
            self._runtime_generation += 1
            runtime_generation = self._runtime_generation
            catalog = self._catalog_locked(ModelInputScopeKind.ROOT)
            subagent_catalog = self._catalog_locked(
                ModelInputScopeKind.SUBAGENT
            )
        policy_by_target = {
            (policy.server_id, policy.remote_tool_name): policy
            for candidate in candidates
            for policy in candidate.ordered_tool_execution_policies
        }
        bindings: list[PreparedToolExecutionBinding] = []
        executors: dict[str, McpBoundToolExecutor] = {}
        for semantic in execution_semantics:
            policy = policy_by_target[
                (semantic.server_id, semantic.remote_tool_name)
            ]
            lease = lease_by_server[semantic.server_id]
            executor_fp = context_fingerprint(
                "mcp-tool-executor-binding:v1",
                {
                    "provider_tool_name": semantic.provider_tool_name,
                    "descriptor_fingerprint": semantic.descriptor_fingerprint,
                    "policy_fingerprint": execution_policy_fingerprint(policy),
                    "slot_lease_identity": lease.lease_identity,
                    "runtime_generation": runtime_generation,
                },
            )
            bindings.append(
                PreparedToolExecutionBinding(
                    tool_name=semantic.provider_tool_name,
                    descriptor_fingerprint=semantic.descriptor_fingerprint,
                    executor_binding_fingerprint=executor_fp,
                    execution_policy=policy,
                )
            )
            executors[semantic.provider_tool_name] = McpBoundToolExecutor(
                runtime_generation_id=runtime_generation,
                semantic=semantic,
                policy=policy,
                lease=lease,
            )
        leases = tuple(dict.fromkeys(lease_by_server.values()))
        for previous_candidate in replaced_candidates:
            previous_candidate.slot_lease.release()
            previous_slot = previous_candidate.slot_lease._slot  # noqa: SLF001
            if previous_slot is self._installed[previous_candidate.server_id].slot_lease._slot:
                continue  # A refreshed catalog still owns this physical connection.
            previous_slot.begin_retire()
            task = asyncio.create_task(
                self._close_retired_slot(previous_slot),
                name=f"mcp-slot-retire:{previous_slot.slot_id}",
            )
            self._slot_close_tasks.add(task)
            task.add_done_callback(self._slot_close_tasks.discard)
        return McpInstalledRuntimeGeneration(
            runtime_generation_id=runtime_generation,
            root_tool_specs=root,
            subagent_tool_specs=child,
            execution_bindings=tuple(bindings),
            executors=executors,
            catalog_snapshot=catalog,
            subagent_catalog_snapshot=subagent_catalog,
            slot_leases=leases,
            slot_lease_by_server=lease_by_server,
            candidates={item.server_id: item for item in candidates},
            root_provider_name_collision_facts=root_collisions,
            subagent_provider_name_collision_facts=child_collisions,
        )

    def catalog_snapshot(self) -> McpCatalogSnapshot:
        with self._lock:
            return self._catalog_locked(ModelInputScopeKind.ROOT)

    def _user_prompt_candidate_locked(self, server_id):
        config = next((c for c in self.configs if c.server_id == server_id and c.enabled), None)
        candidate = self._pending.get(server_id) or self._installed.get(server_id)
        if (self._closed or config is None or candidate is None
                or not _candidate_exact_joins_config(candidate, config, supervisor_epoch=self._epoch)
                or not candidate.slot_lease._slot.lease_is_current(candidate.slot_lease)):
            return None
        return candidate

    def list_user_prompts(self, *, offset=0, limit=200, query=""):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid MCP prompt page")
        from urllib.parse import quote

        if not isinstance(query, str):
            raise ValueError("invalid MCP prompt query")

        with self._lock:
            rows = []
            pending = False
            for config in self.configs:
                if not config.enabled:
                    continue
                candidate = self._user_prompt_candidate_locked(config.server_id)
                if candidate is None:
                    pending = True
                    continue
                for prompt in candidate.discovery_snapshot.prompts:
                    rows.append({
                        **mcp_prompt_public_item(config.server_id, prompt),
                        "target": {"kind": "MCP_PROMPT", "server_id": config.server_id, "name": prompt.name},
                        "server_name": config.display_name,
                    })
            rows.sort(key=lambda row: (row["server_id"], row["name"]))
            needle = query.casefold()
            if "/" in query:
                rows = [row for row in rows if any(path.casefold().startswith(needle) for path in (
                    row["server_id"] + "/" + row["name"],
                    quote(row["server_id"], safe="-_.!~*'()") + "/" + quote(row["name"], safe="-_.!~*'()"),
                ))]
            else:
                rows = [row for row in rows if any(value.casefold().startswith(needle) for value in (
                    row["server_id"], row["name"], quote(row["server_id"], safe="-_.!~*'()"),
                    quote(row["name"], safe="-_.!~*'()")))]
            end = offset + limit
            return {"state": "updating" if pending else "ready", "items": rows[offset:end],
                    "next_offset": end if end < len(rows) else None}

    async def preview_user_prompt(self, target, arguments):
        from .sdk_facade import MCPError

        if (not isinstance(target, Mapping) or set(target) != {"kind", "server_id", "name"}
                or target.get("kind") != "MCP_PROMPT"
                or not all(isinstance(target.get(key), str) and target[key] for key in ("server_id", "name"))
                or not isinstance(arguments, Mapping)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in arguments.items())):
            return {"state": "invalid_arguments", "message": "提示词参数无效。"}
        with self._lock:
            candidate = self._user_prompt_candidate_locked(target["server_id"])
            if candidate is None:
                return {"state": "unavailable", "message": "连接或提示词目录已变化，请重新选择。"}
            prompt = next((p for p in candidate.discovery_snapshot.prompts if p.name == target["name"]), None)
            if prompt is None:
                return {"state": "unavailable", "message": "提示词已不在当前目录中，请重新选择。"}
            declared = {name for name, _, _ in prompt.arguments}
            missing = [name for name, _, required in prompt.arguments
                       if required and not arguments.get(name, "").strip()]
            if missing or set(arguments) - declared:
                return {"state": "invalid_arguments", "message": "请填写声明的必填参数。", "fields": missing}
            slot = candidate.slot_lease._slot
            lease = slot.issue_lease(candidate.slot_lease.admitted_discovery_generation)
        client = slot.client
        owner = McpInputRequiredRoundOwner(operation_identity=random_id("operation"),
                                         connection_generation=slot.connection_generation)
        try:
            async with slot.user_prompt_operation(lease):
                with self._lock:
                    if self._user_prompt_candidate_locked(target["server_id"]) is not candidate:
                        raise McpSnapshotStale("MCP_SNAPSHOT_STALE")
                async with asyncio.timeout(candidate.standard_read_timeout_ms / 1000):
                    result = await _get_prompt_result(client, prompt.name, dict(arguments), owner)
                with self._lock:
                    if self._user_prompt_candidate_locked(target["server_id"]) is not candidate:
                        raise McpSnapshotStale("MCP_SNAPSHOT_STALE")
                if len(result.model_dump_json().encode("utf-8")) > MAXIMUM_MCP_REMOTE_BODY_BYTES:
                    return {"state": "resource_boundary", "message": "提示词超过 MCP 响应大小边界。"}
                rendered = user_prompt_text(result)
                if rendered["state"] == "ready":
                    rendered["text"] = self._credential_boundary.capture_scrub_set().scrub_text(rendered["text"])
                return rendered
        except asyncio.CancelledError:
            raise
        except McpSnapshotStale:
            return {"state": "unavailable", "message": "连接或提示词目录已变化，请重新选择。"}
        except McpProtocolConformanceError as exc:
            slot.report_transport_failure("MCP_PROTOCOL_CONFORMANCE_FAILED", retryable=False)
            return {"state": "failed", "message": client.diagnostic_message(exc)}
        except McpTransportOperationError as exc:
            slot.report_transport_failure("MCP_TRANSPORT_FAILED", retryable=True)
            return {"state": "failed", "message": client.diagnostic_message(exc)}
        except MCPError as exc:
            if _mcp_error_is_transport_failure(exc):
                slot.report_transport_failure("MCP_TRANSPORT_FAILED", retryable=True)
            return {"state": "failed", "message": client.diagnostic_message(exc)}
        except (TimeoutError, McpInputRequiredUnsupported) as exc:
            return {"state": "failed", "message": client.diagnostic_message(exc)}
        except MemoryError:
            raise
        except Exception as exc:
            slot.report_transport_failure("MCP_TRANSPORT_FAILED", retryable=True)
            return {"state": "failed", "message": client.diagnostic_message(exc)}
        finally:
            owner.close()
            lease.release()

    def inspect_discovery_catalog(
        self,
        scope: ModelInputScopeKind = ModelInputScopeKind.ROOT,
        *,
        live: bool = False,
    ) -> McpDiscoveryCatalogInspection:
        """Freeze the latest bounded discovery for management/UI inspection.

        An optional connection may produce a normalized candidate after Host
        publication.  Provider publication still belongs
        exclusively to ``install_pending_at_safe_point``; this read-only view
        lets management surfaces report what the peer actually exposed without
        mutating the current provider/tool generation.

        Management reads use ``live`` to observe the existing physical task and
        slot owners. Retained provider semantics are not connection evidence.
        """

        with self._lock:
            if self._closed:
                raise RuntimeError("MCP supervisor is closed")
            inspection_configs = tuple(
                config
                for config in self.configs
                if scope is ModelInputScopeKind.ROOT
                or config.scope_policy is McpScopePolicy.ROOT_AND_SUBAGENTS
            )
            candidates: dict[str, McpInstallationCandidate] = {}
            for config in inspection_configs:
                candidate = self._pending.get(
                    config.server_id
                ) or self._installed.get(config.server_id)
                if (
                    config.enabled
                    and candidate is not None
                    and _candidate_exact_joins_config(
                        candidate,
                        config,
                        supervisor_epoch=self._epoch,
                    )
                    and (not live or candidate.slot_lease._slot.lease_is_current(  # noqa: SLF001
                        candidate.slot_lease
                    ))
                ):
                    candidates[config.server_id] = candidate
            semantics, _collisions = _aggregate_provider_projection(
                tuple(candidates[key] for key in sorted(candidates)),
                scope,
            )
            policy_by_tool: dict[
                tuple[str, str], McpToolExecutionPolicyFact
            ] = {}
            for candidate in candidates.values():
                for policy in candidate.ordered_tool_execution_policies:
                    if policy.server_id != candidate.server_id:
                        raise RuntimeError(
                            "MCP discovery inspection policy has the wrong server"
                        )
                    key = (policy.server_id, policy.remote_tool_name)
                    if key in policy_by_tool:
                        raise RuntimeError(
                            "MCP discovery inspection policy identity is duplicated"
                        )
                    policy_by_tool[key] = policy
            tools: list[McpCatalogToolInspection] = []
            for semantic in semantics:
                policy = policy_by_tool.get(
                    (semantic.server_id, semantic.remote_tool_name)
                )
                if (
                    policy is None
                    or policy.server_id != semantic.server_id
                    or policy.remote_tool_name != semantic.remote_tool_name
                    or policy.provider_tool_name != semantic.provider_tool_name
                    or policy.tool_semantic_fingerprint
                    != semantic.descriptor_fingerprint
                ):
                    raise RuntimeError(
                        "MCP discovery inspection policy does not join semantic"
                    )
                tools.append(McpCatalogToolInspection(semantic, policy))
            return McpDiscoveryCatalogInspection(
                catalog_snapshot=self._catalog_from_candidates_locked(
                    scope, candidates, live=live,
                ),
                configured_servers=tuple(
                    McpConfiguredServerInspection(
                        server_id=config.server_id,
                        resolved_config_identity=config.resolved_config_identity,
                        source_kind=_mcp_config_source_kind(config),
                        status_matches_config=(
                            live or self._state_config_identity.get(config.server_id)
                            == config.resolved_config_identity
                        ),
                    )
                    for config in inspection_configs
                ),
                tools=tuple(
                    sorted(
                        tools,
                        key=lambda item: (
                            item.semantic.server_id,
                            item.semantic.remote_tool_name,
                        ),
                    )
                ),
            )

    def freeze_capability_source_snapshot_set(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
    ) -> PreparedMcpCapabilitySourceSnapshotSet:
        """Freeze every enabled config source under the supervisor lock."""

        with self._lock:
            if self._closed:
                raise RuntimeError("MCP supervisor is closed")
            snapshots = []
            inspection_inputs: list[PreparedMcpInspectionInput] = []
            scoped_semantics, collision_facts = _aggregate_provider_projection(
                tuple(self._installed[key] for key in sorted(self._installed)),
                conversation_scope_kind,
            )
            semantics_by_server: dict[str, list[McpToolSemanticFact]] = {}
            for semantic in scoped_semantics:
                semantics_by_server.setdefault(semantic.server_id, []).append(semantic)
            for config in self.configs:
                if not config.enabled:
                    continue
                if (
                    conversation_scope_kind is ModelInputScopeKind.SUBAGENT
                    and config.scope_policy is not McpScopePolicy.ROOT_AND_SUBAGENTS
                ):
                    continue
                source = capability_source_ref(
                    CapabilitySourceKind.MCP_SERVER, config.server_id
                )
                registration = capability_source_registration(
                    source=source,
                    refresh_mode=CapabilitySourceRefreshMode.SAFE_POINT_REFRESHABLE,
                    source_contract_fingerprint=context_fingerprint(
                        "mcp-capability-source-contract:v1",
                        {
                            "semantic_config": config.semantic_config_fingerprint,
                            "scope_policy": config.scope_policy.value,
                            "exposure_policy": {
                                "include": config.exposure_policy.include_tool_names,
                                "exclude": config.exposure_policy.exclude_tool_names,
                                "invalid": config.exposure_policy.invalid_tool_policy.value,
                            },
                        },
                    ),
                )
                candidate = self._installed.get(config.server_id)
                clean = (
                    candidate is not None
                    and self._state[config.server_id] is McpServerState.READY
                    and candidate.slot_lease._slot.dispatch_fence_state  # noqa: SLF001
                    is McpDispatchFenceState.OPEN
                )
                facts = ()
                if clean and candidate is not None:
                    server_semantics = tuple(
                        semantics_by_server.get(config.server_id, ())
                    )
                    facts = tuple(
                        freeze_tool_capability_fact(
                            identity=capability_identity(
                                kind=CapabilityKind.TOOL,
                                source=source,
                                stable_name=item.remote_tool_name,
                            ),
                            origin=ToolCapabilityOrigin.MCP,
                            canonical_tool_spec=item.provider_spec(),
                        )
                        for item in server_semantics
                    )
                disposition = (
                    CapabilitySourceSnapshotDisposition.COMPLETE
                    if clean
                    else CapabilitySourceSnapshotDisposition.UNAVAILABLE
                )
                source_snapshot = freeze_capability_source_snapshot(
                        registration=registration,
                        conversation_scope_kind=conversation_scope_kind,
                        disposition=disposition,
                        facts=facts,
                    )
                snapshots.append(source_snapshot)
                if clean and candidate is not None:
                    semantic_by_remote = {
                        item.remote_tool_name: item
                        for item in semantics_by_server.get(config.server_id, ())
                    }
                    policy_by_remote = {
                        item.remote_tool_name: item
                        for item in candidate.ordered_tool_execution_policies
                    }
                    for fact in source_snapshot.facts:
                        if not isinstance(fact, FrozenToolCapabilityFact):
                            raise TypeError("MCP source emitted a non-Tool fact")
                        semantic = semantic_by_remote.get(
                            fact.identity.stable_name
                        )
                        policy = policy_by_remote.get(fact.identity.stable_name)
                        if (
                            semantic is None
                            or policy is None
                            or semantic.descriptor_fingerprint
                            != fact.canonical_tool_spec.descriptor_fingerprint
                            or policy.tool_semantic_fingerprint
                            != fact.canonical_tool_spec.descriptor_fingerprint
                        ):
                            raise RuntimeError(
                                "MCP inspection input does not join source fact"
                            )
                        inspection_inputs.append(
                            PreparedMcpInspectionInput(
                                source_snapshot=source_snapshot,
                                tool_fact=fact,
                                semantic=semantic,
                                policy=policy,
                            )
                        )
            ordered = tuple(
                sorted(
                    snapshots,
                    key=lambda item: item.registration.source.stable_source_id,
                )
            )
            catalog = self._catalog_locked(conversation_scope_kind)
            return issue_mcp_capability_source_snapshot_set(
                conversation_scope_kind=conversation_scope_kind,
                source_snapshots=ordered,
                catalog_snapshot=catalog,
                inspection_inputs=tuple(
                    sorted(
                        inspection_inputs,
                        key=lambda item: (
                            item.semantic.server_id,
                            item.semantic.remote_tool_name,
                        ),
                    )
                ),
                owner_authenticity=self._capability_owner_authenticity,
                provider_name_collision_facts=collision_facts,
            )

    def freeze_capability_projection_input(
        self, owner: PreparedMcpCapabilitySourceSnapshotSet
    ) -> FrozenMcpCapabilityProjectionInput:
        if owner.owner_authenticity is not self._capability_owner_authenticity:
            raise ValueError("foreign MCP capability source snapshot")
        catalog = owner.catalog_snapshot
        inspectability = []
        for item in owner.inspection_inputs:
            semantic = item.semantic
            policy = item.policy
            source_snapshot = item.source_snapshot
            fact = item.tool_fact
            if not any(
                source_snapshot is candidate for candidate in owner.source_snapshots
            ):
                raise ValueError("MCP inspection source snapshot is absent")
            if not any(fact is candidate for candidate in source_snapshot.facts):
                raise ValueError("MCP inspection Tool fact is absent")
            if (
                semantic.server_id
                != fact.identity.source.stable_source_id
                or semantic.remote_tool_name != fact.identity.stable_name
                or semantic.provider_spec() != fact.canonical_tool_spec
                or policy.provider_tool_name
                != fact.canonical_tool_spec.name
                or policy.tool_semantic_fingerprint
                != fact.canonical_tool_spec.descriptor_fingerprint
            ):
                raise ValueError("MCP inspection owner input drifted")
            values = McpInspectionDescriptorValues(
                server_id=semantic.server_id,
                remote_tool_name=semantic.remote_tool_name,
                provider_tool_name=semantic.provider_tool_name,
                description=semantic.description,
                input_schema=semantic.input_schema,
                output_schema=semantic.output_schema,
                effect_kind=policy.effect_kind.value,
            )
            inspectability.append(
                freeze_mcp_inspectability_fact(
                    target=McpToolCapabilityRef(
                        server_id=semantic.server_id,
                        remote_tool_name=semantic.remote_tool_name,
                    ),
                    version=tool_capability_version_ref(fact),
                    descriptor_payload_fingerprint=(
                        mcp_inspection_descriptor_payload_fingerprint(values)
                    ),
                    mcp_execution_policy_fingerprint=execution_policy_fingerprint(policy),
                    effect_kind=McpInspectEffectKind(policy.effect_kind.value),
                    conservative_logical_utf8_bytes=(
                        conservative_mcp_inspection_logical_utf8_bytes(values)
                    ),
                )
            )
        ordered_inspectability = tuple(
            sorted(
                inspectability,
                key=lambda item: (
                    item.target.server_id,
                    item.target.remote_tool_name,
                ),
            )
        )
        return FrozenMcpCapabilityProjectionInput(
            conversation_scope_kind=owner.conversation_scope_kind,
            source_snapshots=owner.source_snapshots,
            catalog_semantic_fingerprint=catalog.semantic_fingerprint,
            inspectability_facts=ordered_inspectability,
            provider_name_collision_facts=(
                owner.provider_name_collision_facts
            ),
        )

    def _catalog_locked(self, scope: ModelInputScopeKind) -> McpCatalogSnapshot:
        return self._catalog_from_candidates_locked(scope, self._installed)

    def _catalog_from_candidates_locked(
        self,
        scope: ModelInputScopeKind,
        candidates: Mapping[str, McpInstallationCandidate],
        *,
        live: bool = False,
    ) -> McpCatalogSnapshot:
        entries: list[McpServerCatalogEntry] = []
        _semantics, collision_facts = _aggregate_provider_projection(
            tuple(candidates[key] for key in sorted(candidates)),
            scope,
        )
        collision_targets = {
            (member.server_id, member.remote_tool_name)
            for fact in collision_facts
            for member in fact.members
        }
        for config in self.configs:
            if (
                scope is ModelInputScopeKind.SUBAGENT
                and config.scope_policy is not McpScopePolicy.ROOT_AND_SUBAGENTS
            ):
                continue
            candidate = candidates.get(config.server_id)
            state, failure = (
                self._live_state_locked(config, candidate)
                if live
                else (self._state[config.server_id], self._failure.get(config.server_id))
            )
            snapshot = (
                scope_mcp_discovery_snapshot(
                    candidate.discovery_snapshot,
                    scope,
                )
                if candidate is not None
                else None
            )
            visible_tools = (
                ()
                if snapshot is None
                else tuple(
                    item
                    for item in snapshot.tools
                    if (item.server_id, item.remote_tool_name)
                    not in collision_targets
                )
            )
            entries.append(
                McpServerCatalogEntry(
                    server_id=config.server_id,
                    runtime_source=(
                        candidate.slot_lease._slot.client.config.runtime_source
                        if candidate is not None
                        else config.runtime_source
                    ),
                    display_name=config.display_name,
                    status=state,
                    exposed_tool_count=len(visible_tools),
                    discovered_tool_count=(
                        snapshot.discovered_tool_count if snapshot else 0
                    ),
                    resource_count=len(snapshot.resources) if snapshot else 0,
                    resource_template_count=(len(snapshot.resource_templates) if snapshot else 0),
                    prompt_count=len(snapshot.prompts) if snapshot else 0,
                    bounded_tool_name_overview=(
                        tuple(
                            item.provider_tool_name for item in visible_tools[:32]
                        )
                    ),
                    sanitized_instructions=(snapshot.sanitized_instructions if snapshot else ""),
                    stable_failure_category=failure,
                    tool_surface_semantic_fingerprint=(
                        snapshot.tool_surface_semantic_fingerprint if snapshot else None
                    ),
                    catalog_semantic_fingerprint=(
                        snapshot.catalog_semantic_fingerprint
                        if snapshot
                        else context_fingerprint(
                            "mcp-catalog-empty-server:v1",
                            (config.server_id, state.value),
                        )
                    ),
                    scope_subagents=(
                        config.scope_policy is McpScopePolicy.ROOT_AND_SUBAGENTS
                    ),
                )
            )
        return build_catalog_snapshot(
            owner_epoch=self._epoch,
            catalog_revision=self._catalog_revision,
            entries=tuple(entries),
            provider_name_collision_facts=collision_facts,
        )

    def _live_state_locked(
        self, config: McpServerConfig, candidate: McpInstallationCandidate | None,
    ) -> tuple[McpServerState, str | None]:
        """Project physical owners without publishing a catalog transition."""
        if not config.enabled:
            return McpServerState.DISABLED, None
        if candidate is not None:
            return McpServerState.READY, None
        task = self._tasks.get(config.server_id)
        if task is not None:
            if not task.done():
                state = self._state[config.server_id]
                return (McpServerState.DISCOVERING if state is McpServerState.DISCOVERING
                        else McpServerState.CONNECTING), self._failure.get(config.server_id)
            if task.cancelled():
                return McpServerState.FAILED_RETRYABLE, "CancelledError"
            error = task.exception()
            if error is not None:
                return McpServerState.FAILED_RETRYABLE, type(error).__name__
            outcome = task.result()
            if outcome.state is not McpServerState.READY:
                return outcome.state, outcome.failure
        # Transport failure already withdraws the candidate and records its
        # stable category. A healthy attempt result cannot overrule that loss.
        state = self._state[config.server_id]
        if (self._state_config_identity.get(config.server_id) == config.resolved_config_identity
                and state in {McpServerState.FAILED_RETRYABLE, McpServerState.FAILED_TERMINAL}):
            return state, self._failure.get(config.server_id)
        slot = self._slots.get(config.server_id)
        if slot is not None:
            if slot.dispatch_fence_state is McpDispatchFenceState.CLOSED:
                return McpServerState.RETIRING, None
            return McpServerState.DISCOVERING, None
        return McpServerState.CONNECTING, None

    def reconnect(self, server_id: str) -> None:
        active_connect: asyncio.Task[_McpConnectOutcome] | None = None
        with self._lock:
            if server_id not in self._config_by_id:
                raise KeyError(server_id)
            retry = self._retry_tasks.get(server_id)
            if retry is not None and not retry.done():
                retry.cancel()
            slot = self._slots.get(server_id)
            if slot is not None:
                # Fence new dispatch immediately; permits admitted before this
                # point may still drain on their exact old slot.
                slot.mark_dirty()
            current = self._tasks.get(server_id)
            if current is not None and not current.done():
                # Invalidate the in-flight attempt before starting its exact
                # replacement.  Cancellation joins/cleans the old physical
                # client in _connect(); no second owner adopts it.
                self._attempt_generation[server_id] += 1
                active_connect = current
                self._tasks.pop(server_id, None)
        if active_connect is not None:
            active_connect.cancel()
        self._start_connect(server_id)

    async def wait_for_server_settlement(
        self, server_id: str, *, timeout_seconds: float
    ) -> McpServerState:
        if timeout_seconds <= 0:
            raise ValueError("MCP settlement timeout must be positive")
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            with self._lock:
                if server_id not in self._config_by_id:
                    raise KeyError(server_id)
                state = self._state[server_id]
            if state in {
                McpServerState.READY,
                McpServerState.FAILED_RETRYABLE,
                McpServerState.FAILED_TERMINAL,
                McpServerState.DISABLED,
            }:
                return state
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("MCP server settlement deadline expired")
            await asyncio.sleep(min(0.01, remaining))

    def stop_admission(self) -> None:
        with self._lock:
            for slot in tuple(self._slots.values()):
                slot.begin_close()

    async def aclose(self) -> None:
        with self._lock:
            if self._close_task is None:
                self._closed = True
                self._configuration_changed.set()
                self._close_task = asyncio.create_task(
                    self._close(), name="mcp-supervisor-physical-close"
                )
            task = self._close_task
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
        task.result()
        if cancelled is not None:
            raise cancelled

    async def _close(self) -> None:
        with self._lock:
            self._closed = True
            tasks = tuple(self._connection_tasks) + tuple(self._refresh_tasks.values())
            tasks += tuple(self._periodic_tasks.values())
            tasks += tuple(self._retry_tasks.values())
            slot_close_tasks = tuple(self._slot_close_tasks)
            slots = tuple(self._all_slots)
            pending = tuple(self._pending.values())
            installed = tuple(self._installed.values())
            config_anchors = tuple(
                item.physical_lifetime_anchor
                for item in self.configs
                if item.physical_lifetime_anchor is not None
            )
            self._pending.clear()
            self._installed.clear()
            for slot in slots:
                slot.begin_close()
        for candidate in pending:
            candidate.slot_lease.release()
        for candidate in installed:
            candidate.slot_lease.release()
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        task_results = await asyncio.gather(*tasks, return_exceptions=True)
        # Terminate exact physical transports first.  Product-operation tasks
        # then leave their lane permits; runtime generations still retain only
        # opaque leases and are released by DirectKernelToolPort.
        close_results = await asyncio.gather(
            *(slot.client.aclose() for slot in slots), return_exceptions=True
        )
        while any(
            slot.active_slot_lease_count
            or slot.active_dispatch_count
            or slot.active_operation_count
            for slot in slots
        ):
            await asyncio.sleep(0.01)
        failures = [value for value in task_results
                    if isinstance(value, BaseException)
                    and not isinstance(value, asyncio.CancelledError)]
        for slot, result in zip(slots, close_results, strict=True):
            if isinstance(result, BaseException):
                failures.append(result)
                if not slot.client.physical_close_complete:
                    continue
            slot.close_physical_lifetime()
            with self._lock:
                self._all_slots.discard(slot)
        # Retiring-slot close tasks are physical owners, not retry timers.  They
        # must be joined rather than cancelled, even when Host close races them.
        if slot_close_tasks:
            retired_results = await asyncio.gather(
                *slot_close_tasks, return_exceptions=True
            )
            failures.extend(value for value in retired_results
                            if isinstance(value, BaseException))
        with self._lock:
            complete = not self._all_slots
        if complete:
            for anchor in config_anchors:
                _close_physical_lifetime_anchor(anchor)
            with self._lock:
                self.configs = ()
                self._config_by_id.clear()
        if failures:
            raise failures[0]


def _duplicate_physical_lifetime_anchor(anchor: object | None) -> object | None:
    if anchor is None:
        return None
    duplicate = getattr(anchor, "duplicate", None)
    if not callable(duplicate):
        raise TypeError("MCP physical lifetime anchor is not duplicable")
    return duplicate()


def _render_typed_result(result: types.CallToolResult) -> bytes:
    payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    if (
        "structured_content" in result.model_fields_set
        and result.structured_content is None
    ):
        # An explicit JSON null is a modern structured result, distinct from
        # an omitted field. exclude_none otherwise erases that observation.
        payload["structuredContent"] = None
    payload.pop("_meta", None)
    data = canonical_json_bytes(payload)
    if len(data) > MAXIMUM_MCP_REMOTE_BODY_BYTES:
        raise RuntimeError("MCP_REMOTE_BODY_BOUND_EXCEEDED")
    return data


__all__ = [
    "McpBoundToolExecutor",
    "McpDispatchAdmissionPermit",
    "McpHostSupervisor",
    "McpInstalledRuntimeGeneration",
    "McpKnownToolResult",
    "McpSlotLease",
    "McpSnapshotStale",
    "mcp_failure_required_facts",
]
