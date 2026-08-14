"""Host-scoped MCP physical owner, discovery, fencing, and exact execution."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
import random
import re
from threading import RLock
from typing import Callable, Mapping
from uuid import uuid4

from jsonschema import validators

from pulsara_agent.mcp_config import (
    McpConfiguredEffect,
    McpInvalidToolPolicy,
    McpScopePolicy,
    McpServerConfig,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
    thaw_json,
)

from ..tool_surface import (
    McpEffectKind,
    McpPolicyClassificationSource,
    McpToolExecutionPolicyFact,
    PreparedToolExecutionBinding,
)
from .contracts import (
    MAXIMUM_CONFIGURED_MCP_SERVERS,
    MAXIMUM_DISCOVERED_TOOLS_PER_SERVER,
    MAXIMUM_DISCOVERY_ITEMS_PER_SERVER,
    MAXIMUM_DISCOVERY_PAGES_PER_METHOD,
    MAXIMUM_MCP_CATALOG_RESULT_BYTES,
    MAXIMUM_MCP_INSTRUCTIONS_BYTES,
    MAXIMUM_MCP_HOST_IN_FLIGHT,
    MAXIMUM_MCP_REMOTE_BODY_BYTES,
    McpCatalogSnapshot,
    McpDiscoverySnapshot,
    McpInstallationCandidate,
    McpPhysicalConcurrencyKind,
    McpPromptSemanticFact,
    McpResourceSemanticFact,
    McpResourceTemplateSemanticFact,
    McpServerCatalogEntry,
    McpServerState,
    McpToolSemanticFact,
    build_catalog_snapshot,
)
from .naming import mangle_mcp_tool_names
from .input_required import (
    McpInputRequiredRoundOwner,
    McpInputRequiredUnsupported,
)
from .sdk_facade import (
    BoundedMcpSdkClient,
    McpAdvertisedCapabilities,
    McpProtocolConformanceError,
    McpTransportOperationError,
    types,
)
from .wire import DEFAULT_MCP_WIRE_BOUNDS, validate_schema


class McpDispatchFenceState(StrEnum):
    OPEN = "OPEN"
    DIRTY_FENCED = "DIRTY_FENCED"
    CLOSED = "CLOSED"


class McpDispatchPermitState(StrEnum):
    ADMITTED = "ADMITTED"
    ATTEMPT_ACCEPTED = "ATTEMPT_ACCEPTED"
    LANE_ACQUIRED = "LANE_ACQUIRED"
    RELEASED = "RELEASED"


class McpSnapshotStale(RuntimeError):
    pass


class McpPhysicalOutcomeUnknown(RuntimeError):
    pass


class _McpOutputSchemaMismatch(ValueError):
    """An exact MCP result contradicts its frozen advertised output schema."""


_DEFAULT_MCP_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_SUPPORTED_MCP_SCHEMA_DIALECTS = frozenset(
    {
        "http://json-schema.org/draft-07/schema#",
        "https://json-schema.org/draft/2019-09/schema",
        _DEFAULT_MCP_SCHEMA_DIALECT,
    }
)


class McpSlotLease:
    """Opaque, non-copyable capability issued by exactly one slot owner."""

    __slots__ = (
        "slot_id",
        "connection_generation",
        "admitted_discovery_generation",
        "admitted_execution_binding_fingerprint",
        "lease_identity",
        "_slot",
        "_authority",
        "_released",
    )

    def __init__(
        self,
        *,
        slot: "McpConnectionSlot",
        authority: object,
        admitted_discovery_generation: int,
    ) -> None:
        if authority is not slot._lease_authority:  # noqa: SLF001
            raise RuntimeError("MCP slot lease construction is sealed")
        self.slot_id = slot.slot_id
        self.connection_generation = slot.connection_generation
        self.admitted_discovery_generation = admitted_discovery_generation
        self.admitted_execution_binding_fingerprint = (
            slot.execution_binding_fingerprint
        )
        self.lease_identity = context_fingerprint(
            "mcp-slot-lease-identity:v1",
            {
                "slot_id": self.slot_id,
                "connection_generation": self.connection_generation,
                "admitted_discovery_generation": self.admitted_discovery_generation,
                "execution_binding_fingerprint": (
                    self.admitted_execution_binding_fingerprint
                ),
            },
        )
        self._slot = slot
        self._authority = authority
        self._released = False

    def exactly_joins(self, slot: "McpConnectionSlot") -> bool:
        return (
            not self._released
            and self._slot is slot
            and self._authority is slot._lease_authority  # noqa: SLF001
            and self.slot_id == slot.slot_id
            and self.connection_generation == slot.connection_generation
            and self.admitted_execution_binding_fingerprint
            == slot.execution_binding_fingerprint
        )

    def release(self) -> None:
        if self._released:
            return
        self._slot.release_lease(self)
        self._released = True

    def __copy__(self) -> None:
        raise TypeError("MCP slot leases cannot be copied")

    def __deepcopy__(self, memo: object) -> None:
        del memo
        raise TypeError("MCP slot leases cannot be copied")


@dataclass(slots=True)
class McpDispatchAdmissionPermit:
    permit_id: str
    session_id: str
    scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    turn_id: str
    tool_call_id: str
    descriptor_fingerprint: str
    policy_fingerprint: str
    runtime_generation_id: int
    admitted_dirty_generation: int
    lease: McpSlotLease = field(repr=False)
    _slot: "McpConnectionSlot" = field(repr=False)
    _authority: object = field(repr=False)
    state: McpDispatchPermitState = McpDispatchPermitState.ADMITTED

    def mark_attempt_accepted(self) -> None:
        self._slot.mark_attempt_accepted(self)

    async def acquire_operation(self) -> "McpPhysicalOperationPermit":
        return await self._slot.acquire_operation(self)

    def release(self) -> None:
        if self.state is McpDispatchPermitState.RELEASED:
            return
        self._slot.release_dispatch(self)


class McpPhysicalOperationPermit:
    def __init__(
        self,
        permit: McpDispatchAdmissionPermit,
        semaphore: asyncio.Semaphore,
        host_semaphore: asyncio.Semaphore,
    ) -> None:
        self.permit = permit
        self._semaphore = semaphore
        self._host_semaphore = host_semaphore
        self._released = False

    async def __aenter__(self) -> "McpPhysicalOperationPermit":
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._semaphore.release()
        self._host_semaphore.release()
        self.permit._slot.release_operation(self.permit)  # noqa: SLF001


class McpConnectionSlot:
    def __init__(
        self,
        *,
        server_id: str,
        supervisor_epoch: int,
        connection_generation: int,
        runtime_config_fingerprint: str,
        client: BoundedMcpSdkClient,
        concurrency_kind: McpPhysicalConcurrencyKind,
        maximum_in_flight: int,
        host_lane: asyncio.Semaphore,
        failure_reporter: Callable[["McpConnectionSlot", str, bool], None],
    ) -> None:
        self.slot_id = f"mcp-slot:{server_id}:{uuid4().hex}"
        self.server_id = server_id
        self.supervisor_epoch = supervisor_epoch
        self.connection_generation = connection_generation
        self.runtime_config_fingerprint = runtime_config_fingerprint
        self.client = client
        self.concurrency_kind = concurrency_kind
        self.execution_binding_fingerprint = context_fingerprint(
            "mcp-slot-execution-binding:v1",
            {
                "slot_id": self.slot_id,
                "server_id": server_id,
                "supervisor_epoch": supervisor_epoch,
                "connection_generation": connection_generation,
                "runtime_config_fingerprint": runtime_config_fingerprint,
                "concurrency_kind": concurrency_kind.value,
            },
        )
        self._lock = RLock()
        self._lease_authority = object()
        self._permit_authority = object()
        self._lane = asyncio.Semaphore(maximum_in_flight)
        self._host_lane = host_lane
        self._failure_reporter = failure_reporter
        self._dirty_generation = 0
        self._dispatch_fence = McpDispatchFenceState.OPEN
        self._accepting_new_leases = True
        self._leases: set[McpSlotLease] = set()
        self._permits: dict[str, McpDispatchAdmissionPermit] = {}
        self._active_operation_count = 0

    def report_transport_failure(
        self, category: str, *, retryable: bool
    ) -> None:
        # The exact physical slot is fenced even when a newer candidate has
        # already become the supervisor's reconnect target.  Global status is
        # updated only if this slot is still current.
        self.mark_dirty()
        self._failure_reporter(self, category, retryable)

    def configure_physical_concurrency(
        self,
        *,
        concurrency_kind: McpPhysicalConcurrencyKind,
        maximum_in_flight: int,
    ) -> None:
        """Freeze the negotiated lane before discovery leases are issued."""

        with self._lock:
            if self._leases or self._permits or self._active_operation_count:
                raise RuntimeError("MCP physical lane is already in use")
            if maximum_in_flight < 1:
                raise ValueError("MCP physical lane bound must be positive")
            self.concurrency_kind = concurrency_kind
            self._lane = asyncio.Semaphore(maximum_in_flight)
            self.execution_binding_fingerprint = context_fingerprint(
                "mcp-slot-execution-binding:v1",
                {
                    "slot_id": self.slot_id,
                    "server_id": self.server_id,
                    "supervisor_epoch": self.supervisor_epoch,
                    "connection_generation": self.connection_generation,
                    "runtime_config_fingerprint": self.runtime_config_fingerprint,
                    "concurrency_kind": concurrency_kind.value,
                },
            )

    @property
    def dirty_generation(self) -> int:
        with self._lock:
            return self._dirty_generation

    @property
    def active_slot_lease_count(self) -> int:
        with self._lock:
            return len(self._leases)

    @property
    def active_operation_count(self) -> int:
        with self._lock:
            return self._active_operation_count

    @property
    def active_dispatch_count(self) -> int:
        with self._lock:
            return len(self._permits)

    def issue_lease(self, discovery_generation: int) -> McpSlotLease:
        with self._lock:
            if not self._accepting_new_leases or self._dispatch_fence is McpDispatchFenceState.CLOSED:
                raise RuntimeError("MCP slot refuses new leases")
            lease = McpSlotLease(
                slot=self,
                authority=self._lease_authority,
                admitted_discovery_generation=discovery_generation,
            )
            self._leases.add(lease)
            return lease

    def release_lease(self, lease: McpSlotLease) -> None:
        with self._lock:
            if lease._slot is not self or lease._authority is not self._lease_authority:  # noqa: SLF001
                raise RuntimeError("MCP slot lease authority conflicts")
            self._leases.discard(lease)

    def mark_dirty(self) -> int:
        with self._lock:
            if self._dispatch_fence is McpDispatchFenceState.CLOSED:
                return self._dirty_generation
            self._dirty_generation += 1
            self._dispatch_fence = McpDispatchFenceState.DIRTY_FENCED
            self._accepting_new_leases = False
            return self._dirty_generation

    def begin_retire(self) -> None:
        """Stop new leases while existing runtime generations drain normally."""

        with self._lock:
            self._accepting_new_leases = False

    def lease_is_current(self, lease: McpSlotLease) -> bool:
        with self._lock:
            return (
                lease.exactly_joins(self)
                and self._dispatch_fence is McpDispatchFenceState.OPEN
                and lease.admitted_discovery_generation == self._dirty_generation
            )

    def admit_dispatch(
        self,
        *,
        lease: McpSlotLease,
        session_id: str,
        scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        turn_id: str,
        tool_call_id: str,
        descriptor_fingerprint: str,
        policy_fingerprint: str,
        runtime_generation_id: int,
    ) -> McpDispatchAdmissionPermit:
        with self._lock:
            if (
                not lease.exactly_joins(self)
                or self._dispatch_fence is not McpDispatchFenceState.OPEN
                or lease.admitted_discovery_generation != self._dirty_generation
            ):
                raise McpSnapshotStale("MCP_SNAPSHOT_STALE")
            permit = McpDispatchAdmissionPermit(
                permit_id=f"mcp-dispatch:{uuid4().hex}",
                session_id=session_id,
                scope_kind=scope_kind,
                scope_subagent_task_id=scope_subagent_task_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                descriptor_fingerprint=descriptor_fingerprint,
                policy_fingerprint=policy_fingerprint,
                runtime_generation_id=runtime_generation_id,
                admitted_dirty_generation=self._dirty_generation,
                lease=lease,
                _slot=self,
                _authority=self._permit_authority,
            )
            self._permits[permit.permit_id] = permit
            return permit

    def _require_permit(self, permit: McpDispatchAdmissionPermit) -> None:
        if (
            permit._slot is not self
            or permit._authority is not self._permit_authority
            or self._permits.get(permit.permit_id) is not permit
            or permit.lease._slot is not self  # noqa: SLF001
        ):
            raise RuntimeError("MCP dispatch permit authority conflicts")

    def mark_attempt_accepted(self, permit: McpDispatchAdmissionPermit) -> None:
        with self._lock:
            self._require_permit(permit)
            if permit.state is not McpDispatchPermitState.ADMITTED:
                raise RuntimeError("MCP dispatch permit is not admissible")
            permit.state = McpDispatchPermitState.ATTEMPT_ACCEPTED

    async def acquire_operation(
        self, permit: McpDispatchAdmissionPermit
    ) -> McpPhysicalOperationPermit:
        with self._lock:
            self._require_permit(permit)
            if permit.state is not McpDispatchPermitState.ATTEMPT_ACCEPTED:
                raise RuntimeError("MCP attempt is not accepted")
        await self._host_lane.acquire()
        try:
            await self._lane.acquire()
        except BaseException:
            self._host_lane.release()
            raise
        with self._lock:
            try:
                self._require_permit(permit)
                if permit.state is not McpDispatchPermitState.ATTEMPT_ACCEPTED:
                    raise RuntimeError("MCP operation permit raced settlement")
                permit.state = McpDispatchPermitState.LANE_ACQUIRED
                self._active_operation_count += 1
            except BaseException:
                self._lane.release()
                self._host_lane.release()
                raise
        return McpPhysicalOperationPermit(permit, self._lane, self._host_lane)

    def release_dispatch(self, permit: McpDispatchAdmissionPermit) -> None:
        with self._lock:
            self._require_permit(permit)
            if permit.state is McpDispatchPermitState.LANE_ACQUIRED:
                raise RuntimeError("MCP lane permit must settle the operation")
            permit.state = McpDispatchPermitState.RELEASED
            self._permits.pop(permit.permit_id, None)

    def release_operation(self, permit: McpDispatchAdmissionPermit) -> None:
        with self._lock:
            self._require_permit(permit)
            if permit.state is not McpDispatchPermitState.LANE_ACQUIRED:
                raise RuntimeError("MCP operation permit is not active")
            self._active_operation_count -= 1
            permit.state = McpDispatchPermitState.RELEASED
            self._permits.pop(permit.permit_id, None)

    def begin_close(self) -> None:
        with self._lock:
            self._accepting_new_leases = False
            self._dispatch_fence = McpDispatchFenceState.CLOSED
            for permit in tuple(self._permits.values()):
                if permit.state is McpDispatchPermitState.ADMITTED:
                    permit.state = McpDispatchPermitState.RELEASED
                    self._permits.pop(permit.permit_id, None)


@dataclass(frozen=True, slots=True)
class McpKnownToolResult:
    state: str
    content: bytes
    remote_identity: str


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
        scope_subagent_task_id: str | None,
        turn_id: str,
        tool_call_id: str,
    ) -> McpDispatchAdmissionPermit:
        return self.lease._slot.admit_dispatch(  # noqa: SLF001
            lease=self.lease,
            session_id=session_id,
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            descriptor_fingerprint=self.semantic.descriptor_fingerprint,
            policy_fingerprint=self.policy.policy_fingerprint,
            runtime_generation_id=self.runtime_generation_id,
        )

    async def invoke(
        self,
        permit: McpDispatchAdmissionPermit,
        arguments: Mapping[str, object],
    ) -> McpKnownToolResult:
        if (
            permit.runtime_generation_id != self.runtime_generation_id
            or permit.descriptor_fingerprint != self.semantic.descriptor_fingerprint
            or permit.policy_fingerprint != self.policy.policy_fingerprint
            or permit.tool_call_id == ""
        ):
            raise RuntimeError("MCP dispatch permit does not exact-join executor")
        try:
            operation = await permit.acquire_operation()
        except BaseException:
            if permit.state is McpDispatchPermitState.ATTEMPT_ACCEPTED:
                permit.release()
            raise
        async with operation:
            input_owner = McpInputRequiredRoundOwner(
                operation_identity=permit.permit_id,
                connection_generation=self.lease.connection_generation,
            )
            remote_identity = _mcp_remote_identity(
                semantic=self.semantic,
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
                                )
                            # tools/call already crossed the physical effect
                            # boundary.  Without a terminal decline/final
                            # result an external-effect operation is unknown,
                            # never a fabricated known SYSTEM_ERROR.
                            raise McpPhysicalOutcomeUnknown(
                                "MCP external effect requested unsupported input"
                            ) from exc
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
                except McpPhysicalOutcomeUnknown:
                    raise
                except McpProtocolConformanceError:
                    self.lease._slot.report_transport_failure(  # noqa: SLF001
                        "MCP_PROTOCOL_CONFORMANCE_FAILED", retryable=False
                    )
                    return _known_mcp_system_failure(
                        error_code="MCP_RESULT_TYPE_CONFORMANCE_FAILED",
                        remote_identity=remote_identity,
                    )
                except McpTransportOperationError as exc:
                    self.lease._slot.report_transport_failure(  # noqa: SLF001
                        "MCP_TRANSPORT_FAILED", retryable=True
                    )
                    if not exc.may_have_reached_server:
                        return _known_mcp_system_failure(
                            error_code="MCP_TRANSPORT_UNWRITTEN",
                            remote_identity=remote_identity,
                        )
                    raise McpPhysicalOutcomeUnknown(
                        "MCP physical outcome cannot be proved"
                    ) from exc
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    self.lease._slot.report_transport_failure(  # noqa: SLF001
                        "MCP_TRANSPORT_FAILED", retryable=True
                    )
                    raise McpPhysicalOutcomeUnknown(
                        "MCP physical outcome cannot be proved"
                    ) from exc

                # At this boundary an exact response is already present.  Any
                # carrier/rendering failure is therefore a known protocol
                # failure, never an ambiguous physical effect and never a
                # reason to replay tools/call.
                try:
                    if not isinstance(result, types.CallToolResult):
                        raise McpProtocolConformanceError(
                            "MCP_RESULT_TYPE_PAYLOAD_CONTRADICTION"
                        )
                    _validate_tool_output_schema(self.semantic, result)
                    body = _render_typed_result(result)
                except _McpOutputSchemaMismatch:
                    return _known_mcp_system_failure(
                        error_code="MCP_OUTPUT_SCHEMA_MISMATCH",
                        remote_identity=remote_identity,
                    )
                except BaseException:
                    return _known_mcp_system_failure(
                        error_code="MCP_RESULT_LOWERING_FAILED",
                        remote_identity=remote_identity,
                    )
                return McpKnownToolResult(
                    state="APPLICATION_ERROR" if result.is_error else "SUCCESS",
                    content=body,
                    remote_identity=remote_identity,
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
    slot_leases: tuple[McpSlotLease, ...] = field(repr=False, compare=False)
    slot_lease_by_server: Mapping[str, McpSlotLease] = field(
        repr=False, compare=False
    )
    candidates: Mapping[str, McpInstallationCandidate] = field(
        repr=False, compare=False
    )

    def release(self) -> None:
        for lease in self.slot_leases:
            lease.release()

    def standard_operation_server_id(
        self, tool_name: str, arguments: Mapping[str, object]
    ) -> str | None:
        if tool_name in {
            "read_mcp_resource",
            "get_mcp_prompt",
        }:
            value = arguments.get("server_id")
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
        scope_subagent_task_id: str | None,
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
            scope_subagent_task_id=scope_subagent_task_id,
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
            for item in self.catalog_snapshot.for_scope(scope_kind).servers
        }
        if server_id not in visible_server_ids:
            raise ValueError("MCP server is not visible in this scope")
        candidate = self.candidates.get(server_id)
        if candidate is None:
            raise McpSnapshotStale("MCP server is not installed")
        if tool_name == "read_mcp_resource":
            uri = arguments.get("uri")
            if not isinstance(uri, str) or not _resource_uri_in_snapshot(
                uri, candidate.discovery_snapshot
            ):
                raise ValueError(
                    "MCP resource URI is absent from the exact snapshot"
                )
            return candidate
        if tool_name != "get_mcp_prompt":
            raise ValueError("unknown MCP standard operation")
        prompt_name = arguments.get("prompt_name")
        if not isinstance(prompt_name, str):
            raise ValueError("MCP prompt name is invalid")
        prompt = next(
            (
                item
                for item in candidate.discovery_snapshot.prompts
                if item.name == prompt_name
            ),
            None,
        )
        if prompt is None:
            raise ValueError("MCP prompt is absent from the exact snapshot")
        raw_arguments = arguments.get("arguments")
        if raw_arguments is not None and not isinstance(raw_arguments, Mapping):
            raise ValueError("MCP prompt arguments must be an object")
        if isinstance(raw_arguments, Mapping) and any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_arguments.items()
        ):
            raise ValueError("MCP prompt arguments must contain strings")
        supplied = set(raw_arguments or {})
        declared = {name for name, _description, _required in prompt.arguments}
        required = {
            name
            for name, _description, is_required in prompt.arguments
            if is_required
        }
        if not required.issubset(supplied) or not supplied.issubset(declared):
            raise ValueError("MCP prompt arguments do not exact-join discovery")
        return candidate

    async def invoke_standard(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        permit: McpDispatchAdmissionPermit | None,
        scope_kind: ModelInputScopeKind,
    ) -> McpKnownToolResult:
        if tool_name == "list_mcp_servers":
            payload = _bounded_server_catalog_payload(
                self.catalog_snapshot.for_scope(scope_kind)
            )
            return _local_known(payload, tool_name)
        if tool_name in {
            "list_mcp_resources",
            "list_mcp_resource_templates",
            "list_mcp_prompts",
        }:
            return _local_known(
                _list_catalog_items(self, tool_name, arguments, scope_kind),
                tool_name,
            )
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
        if candidate is None or permit.runtime_generation_id != self.runtime_generation_id:
            permit.release()
            raise McpSnapshotStale("MCP standard operation generation is stale")
        permit.mark_attempt_accepted()
        try:
            operation = await permit.acquire_operation()
        except BaseException:
            if permit.state is McpDispatchPermitState.ATTEMPT_ACCEPTED:
                permit.release()
            raise
        async with operation:
            runtime_lease = self.slot_lease_by_server.get(candidate.server_id)
            if runtime_lease is None:
                raise McpSnapshotStale("MCP runtime lease is unavailable")
            client = runtime_lease._slot.client  # noqa: SLF001
            remote_identity = (
                f"mcp:{candidate.server_id}:"
                f"{runtime_lease.connection_generation}:{tool_name}"
            )
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
                except McpProtocolConformanceError:
                    runtime_lease._slot.report_transport_failure(  # noqa: SLF001
                        "MCP_PROTOCOL_CONFORMANCE_FAILED", retryable=False
                    )
                    return _known_mcp_system_failure(
                        error_code="MCP_RESULT_TYPE_CONFORMANCE_FAILED",
                        remote_identity=remote_identity,
                    )
                except McpTransportOperationError as exc:
                    runtime_lease._slot.report_transport_failure(  # noqa: SLF001
                        "MCP_TRANSPORT_FAILED", retryable=True
                    )
                    if not exc.may_have_reached_server:
                        return _known_mcp_system_failure(
                            error_code="MCP_TRANSPORT_UNWRITTEN",
                            remote_identity=remote_identity,
                        )
                    raise McpPhysicalOutcomeUnknown(
                        "MCP physical outcome cannot be proved"
                    ) from exc
                except McpInputRequiredUnsupported:
                    return _known_mcp_system_failure(
                        error_code="MCP_INPUT_REQUIRED_UNSUPPORTED",
                        remote_identity=remote_identity,
                    )
                except TimeoutError:
                    # Standard resource/prompt operations are frozen read-only
                    # product capabilities.  Their bounded timeout is a known
                    # observation failure, never an unknown external effect.
                    return _known_mcp_system_failure(
                        error_code="MCP_STANDARD_READ_TIMEOUT",
                        remote_identity=remote_identity,
                    )
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    runtime_lease._slot.report_transport_failure(  # noqa: SLF001
                        "MCP_TRANSPORT_FAILED", retryable=True
                    )
                    raise McpPhysicalOutcomeUnknown(
                        "MCP physical outcome cannot be proved"
                    ) from exc
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
            except BaseException:
                # resources/read and prompts/get already returned an exact
                # response.  A carrier/rendering bound failure is known and
                # must not be rewritten into physical ambiguity or replayed.
                return _known_mcp_system_failure(
                    error_code="MCP_RESULT_LOWERING_FAILED",
                    remote_identity=remote_identity,
                )
            return McpKnownToolResult(
                state="SUCCESS",
                content=body,
                remote_identity=remote_identity,
            )


async def _invoke_standard_remote_result(
    *,
    client: BoundedMcpSdkClient,
    candidate: McpInstallationCandidate,
    tool_name: str,
    arguments: Mapping[str, object],
    input_owner: McpInputRequiredRoundOwner,
) -> types.ReadResourceResult | types.GetPromptResult:
    request_state: str | None = None
    if tool_name == "read_mcp_resource":
        uri = str(arguments["uri"])
        if not _resource_uri_in_snapshot(uri, candidate.discovery_snapshot):
            raise ValueError("MCP resource URI is absent from the exact snapshot")
        while True:
            result = await client.session.read_resource(
                uri,
                input_responses={} if request_state is not None else None,
                request_state=request_state,
                allow_input_required=True,
            )
            result_type = client.require_closed_result_type(result)
            if result_type == "complete":
                if not isinstance(result, types.ReadResourceResult):
                    raise McpProtocolConformanceError(
                        "MCP_RESULT_TYPE_PAYLOAD_CONTRADICTION"
                    )
                return result
            if not isinstance(result, types.InputRequiredResult):
                raise McpProtocolConformanceError(
                    "MCP_RESULT_TYPE_PAYLOAD_CONTRADICTION"
                )
            request_state = input_owner.prepare_state_only_continuation(result)
    if tool_name != "get_mcp_prompt":
        raise RuntimeError("unknown MCP standard operation")
    prompt_name = str(arguments["prompt_name"])
    prompt = next(
        (
            item
            for item in candidate.discovery_snapshot.prompts
            if item.name == prompt_name
        ),
        None,
    )
    if prompt is None:
        raise ValueError("MCP prompt is absent from the exact snapshot")
    raw_arguments = arguments.get("arguments")
    if raw_arguments is not None and not isinstance(raw_arguments, Mapping):
        raise ValueError("MCP prompt arguments must be an object")
    prompt_arguments = (
        {
            str(key): str(value)
            for key, value in raw_arguments.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if isinstance(raw_arguments, Mapping)
        else None
    )
    if isinstance(raw_arguments, Mapping) and len(prompt_arguments or {}) != len(
        raw_arguments
    ):
        raise ValueError("MCP prompt arguments must contain strings")
    supplied = set(prompt_arguments or {})
    declared = {name for name, _description, _required in prompt.arguments}
    required = {
        name
        for name, _description, is_required in prompt.arguments
        if is_required
    }
    if not required.issubset(supplied) or not supplied.issubset(declared):
        raise ValueError("MCP prompt arguments do not exact-join discovery")
    while True:
        result = await client.session.get_prompt(
            prompt_name,
            prompt_arguments,
            input_responses={} if request_state is not None else None,
            request_state=request_state,
            allow_input_required=True,
        )
        result_type = client.require_closed_result_type(result)
        if result_type == "complete":
            if not isinstance(result, types.GetPromptResult):
                raise McpProtocolConformanceError(
                    "MCP_RESULT_TYPE_PAYLOAD_CONTRADICTION"
                )
            return result
        if not isinstance(result, types.InputRequiredResult):
            raise McpProtocolConformanceError(
                "MCP_RESULT_TYPE_PAYLOAD_CONTRADICTION"
            )
        request_state = input_owner.prepare_state_only_continuation(result)


class McpHostSupervisor:
    """The only owner allowed to connect, fence, and close MCP slots."""

    def __init__(
        self,
        *,
        session_id: str,
        workspace_root: Path,
        configs: tuple[McpServerConfig, ...],
        client_factory: type[BoundedMcpSdkClient] = BoundedMcpSdkClient,
        required_startup_timeout_seconds: float = 120.0,
        optional_fast_start_seconds: float = 3.0,
        connect_attempt_timeout_seconds: float = 120.0,
    ) -> None:
        if len(configs) > MAXIMUM_CONFIGURED_MCP_SERVERS:
            raise ValueError("too many configured MCP servers")
        self.session_id = session_id
        self.workspace_root = workspace_root.resolve()
        self.configs = tuple(sorted(configs, key=lambda item: item.server_id))
        self._config_by_id = {item.server_id: item for item in self.configs}
        self._client_factory = client_factory
        if min(
            required_startup_timeout_seconds,
            optional_fast_start_seconds,
            connect_attempt_timeout_seconds,
        ) <= 0:
            raise ValueError("MCP startup watchdogs must be positive")
        self._required_startup_timeout_seconds = required_startup_timeout_seconds
        self._optional_fast_start_seconds = optional_fast_start_seconds
        self._connect_attempt_timeout_seconds = connect_attempt_timeout_seconds
        self._epoch = int.from_bytes(uuid4().bytes[:8], "big")
        self._lock = RLock()
        self._state = {
            item.server_id: (
                McpServerState.CONNECTING if item.enabled else McpServerState.DISABLED
            )
            for item in self.configs
        }
        self._failure: dict[str, str] = {}
        self._attempt_generation = {item.server_id: 0 for item in self.configs}
        self._refresh_generation = {item.server_id: 0 for item in self.configs}
        self._slots: dict[str, McpConnectionSlot] = {}
        self._all_slots: set[McpConnectionSlot] = set()
        self._slot_close_tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[str, McpInstallationCandidate] = {}
        self._installed: dict[str, McpInstallationCandidate] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
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

    @property
    def owner_epoch(self) -> int:
        return self._epoch

    async def start(self) -> None:
        enabled = tuple(item for item in self.configs if item.enabled)
        for config in enabled:
            self._start_connect(config.server_id)
            if config.catalog_refresh_interval_ms is not None:
                self._periodic_tasks[config.server_id] = asyncio.create_task(
                    self._periodic_refresh(config.server_id),
                    name=f"mcp-periodic-refresh:{config.server_id}",
                )
        required = tuple(self._tasks[item.server_id] for item in enabled if item.required)
        if required:
            await self._wait_for_required_startup(
                tuple(item.server_id for item in enabled if item.required)
            )
        optional = tuple(self._tasks[item.server_id] for item in enabled if not item.required)
        if optional:
            done, _ = await asyncio.wait(
                optional, timeout=self._optional_fast_start_seconds
            )
            for task in done:
                with suppress(Exception):
                    task.result()

    async def _wait_for_required_startup(
        self, server_ids: tuple[str, ...]
    ) -> None:
        deadline = asyncio.get_running_loop().time() + (
            self._required_startup_timeout_seconds
        )
        while True:
            with self._lock:
                states = {server_id: self._state[server_id] for server_id in server_ids}
            if all(state is McpServerState.READY for state in states.values()):
                return
            terminal = tuple(
                server_id
                for server_id, state in states.items()
                if state in {McpServerState.FAILED_TERMINAL, McpServerState.DISABLED}
            )
            if terminal:
                raise RuntimeError(
                    "required MCP server failed: " + ", ".join(terminal)
                )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("required MCP startup deadline expired")
            await asyncio.sleep(min(0.05, remaining))

    def reload_configs(
        self, configs: tuple[McpServerConfig, ...]
    ) -> frozenset[str]:
        """Install one typed config epoch; physical work starts outside the lock."""

        if len(configs) > MAXIMUM_CONFIGURED_MCP_SERVERS:
            raise ValueError("too many configured MCP servers")
        ordered = tuple(sorted(configs, key=lambda item: item.server_id))
        updated = {item.server_id: item for item in ordered}
        if len(updated) != len(ordered):
            raise ValueError("MCP server ids are not unique")
        reconnect: list[str] = []
        retire_slots: list[McpConnectionSlot] = []
        release_candidates: list[McpInstallationCandidate] = []
        cancel_tasks: list[asyncio.Task[None]] = []
        with self._lock:
            if self._closed:
                raise RuntimeError("MCP supervisor is closed")
            old = self._config_by_id
            changed_ids = {
                server_id
                for server_id in set(old) | set(updated)
                if server_id not in old
                or server_id not in updated
                or old[server_id].resolved_config_identity
                != updated[server_id].resolved_config_identity
                or old[server_id].enabled != updated[server_id].enabled
            }
            self.configs = ordered
            self._config_by_id = updated
            for server_id in changed_ids:
                old_config = old.get(server_id)
                new_config = updated.get(server_id)
                retain_semantic_surface = (
                    old_config is not None
                    and new_config is not None
                    and old_config.enabled
                    and new_config.enabled
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
                    self._failure.pop(server_id, None)
                    self._retry_counts.pop(server_id, None)
                    self._refresh_generation.pop(server_id, None)
                else:
                    self._failure.pop(server_id, None)
                    self._retry_counts[server_id] = 0
                    self._state[server_id] = (
                        McpServerState.CONNECTING
                        if config.enabled
                        else McpServerState.DISABLED
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
                if config.enabled and server_id not in reconnect:
                    reconnect.append(server_id)
        for candidate in release_candidates:
            candidate.slot_lease.release()
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
            # A replacement attempt is operational state only while an exact
            # installed generation remains usable.  Publishing CONNECTING here
            # would create a false catalog semantic transition on every
            # periodic refresh even when discovery is identical.
            if not self._installed_available_locked(server_id):
                self._state[server_id] = McpServerState.CONNECTING
            task = asyncio.create_task(
                self._connect(server_id, generation, refresh_generation),
                name=f"mcp-connect:{server_id}:{generation}",
            )
            self._tasks[server_id] = task
            task.add_done_callback(self._consume_connection_outcome)

    @staticmethod
    def _consume_connection_outcome(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        with suppress(BaseException):
            task.result()

    def _installed_available_locked(self, server_id: str) -> bool:
        candidate = self._installed.get(server_id)
        return candidate is not None and candidate.slot_lease._slot.lease_is_current(  # noqa: SLF001
            candidate.slot_lease
        )

    async def _connect(
        self,
        server_id: str,
        attempt_generation: int,
        refresh_generation: int,
    ) -> None:
        config = self._config_by_id[server_id]
        box: dict[str, McpConnectionSlot] = {}

        async def notification(method: str) -> None:
            slot = box.get("slot")
            if slot is None:
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

        client = self._client_factory(
            config,
            workspace_root=self.workspace_root,
            notification_callback=notification,
        )
        slot: McpConnectionSlot | None = None
        candidate_lease: McpSlotLease | None = None
        try:
            # Start serial while the handshake determines whether an HTTP peer
            # installed session state.  Only an operator assertion *and* a
            # session-id-free negotiated transport can open the bounded lane.
            mode = McpPhysicalConcurrencyKind.SERIAL_SESSION
            maximum = 1
            slot = McpConnectionSlot(
                server_id=server_id,
                supervisor_epoch=self._epoch,
                connection_generation=attempt_generation,
                runtime_config_fingerprint=config.runtime_config_fingerprint,
                client=client,
                concurrency_kind=mode,
                maximum_in_flight=maximum,
                host_lane=self._host_operation_lane,
                failure_reporter=self._report_slot_failure,
            )
            box["slot"] = slot
            superseded_pending: McpInstallationCandidate | None = None
            async with asyncio.timeout(self._connect_attempt_timeout_seconds):
                # ``client.open`` already performs initialize/discover and may
                # retain a bounded server response.  Acquire the Host aggregate
                # reservation before that first parse and keep it until the
                # normalized installation candidate is quoted and published.
                async with self._discovery_reservations:
                    await client.open()
                    if client.supports_bounded_stateless_parallelism:
                        mode = McpPhysicalConcurrencyKind.BOUNDED_STATELESS_HTTP
                        maximum = config.stateless_http_max_in_flight
                        slot.configure_physical_concurrency(
                            concurrency_kind=mode,
                            maximum_in_flight=maximum,
                        )
                    with self._lock:
                        if (
                            self._closed
                            or self._attempt_generation[server_id]
                            != attempt_generation
                            or self._refresh_generation[server_id]
                            != refresh_generation
                        ):
                            raise RuntimeError("stale MCP connection attempt")
                        if not self._installed_available_locked(server_id):
                            self._state[server_id] = McpServerState.DISCOVERING
                    snapshot, policies = await _discover(client, config)
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
                        self._all_slots.add(slot)
                        self._pending[server_id] = candidate
                        self._state[server_id] = McpServerState.READY
                        self._failure.pop(server_id, None)
                        self._retry_counts[server_id] = 0
            if superseded_pending is not None:
                superseded_pending.slot_lease.release()
                superseded_slot = superseded_pending.slot_lease._slot  # noqa: SLF001
                superseded_slot.begin_close()
                task = asyncio.create_task(
                    self._close_retired_slot(superseded_slot),
                    name=f"mcp-slot-retire:{superseded_slot.slot_id}",
                )
                self._slot_close_tasks.add(task)
                task.add_done_callback(self._slot_close_tasks.discard)
        except BaseException as exc:
            if candidate_lease is not None:
                candidate_lease.release()
            if slot is not None:
                slot.begin_close()
            await client.aclose()
            with self._lock:
                if (
                    not self._closed
                    and self._attempt_generation[server_id] == attempt_generation
                ):
                    installed_available = self._installed_available_locked(server_id)
                    if installed_available:
                        # A failed replacement must not withdraw the still-live
                        # installed surface or publish an operational failure as
                        # catalog semantics.
                        self._state[server_id] = McpServerState.READY
                        self._failure.pop(server_id, None)
                    else:
                        self._state[server_id] = (
                            McpServerState.FAILED_TERMINAL
                            if isinstance(exc, ValueError)
                            else McpServerState.FAILED_RETRYABLE
                        )
                        self._failure[server_id] = type(exc).__name__
                    if not isinstance(exc, ValueError):
                        self._schedule_retry_locked(server_id)

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
            pending_replacement = self._pending.get(server_id)
            if replacement_available or pending_replacement is not None:
                # The failed exact slot is operational history.  A healthy
                # installed surface (or ready pending replacement) remains the
                # semantic catalog truth.
                self._state[server_id] = McpServerState.READY
                self._failure.pop(server_id, None)
            else:
                self._failure[server_id] = category
                self._state[server_id] = (
                    McpServerState.FAILED_RETRYABLE
                    if retryable
                    else McpServerState.FAILED_TERMINAL
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
        # Coalesce a burst of listChanged notifications into one full relist.
        # A later notification may still fence an in-flight relist through the
        # refresh generation join, but it cannot recursively start discovery
        # from the passive receive callback.
        await asyncio.sleep(0.01)
        self._start_connect(server_id)

    async def _periodic_refresh(self, server_id: str) -> None:
        config = self._config_by_id[server_id]
        assert config.catalog_refresh_interval_ms is not None
        interval = config.catalog_refresh_interval_ms / 1000
        while True:
            await asyncio.sleep(interval)
            with self._lock:
                if (
                    self._closed
                    or server_id not in self._config_by_id
                    or not self._config_by_id[server_id].enabled
                ):
                    return
            self._start_connect(server_id)

    async def _close_retired_slot(self, slot: McpConnectionSlot) -> None:
        while (
            slot.active_slot_lease_count
            or slot.active_dispatch_count
            or slot.active_operation_count
        ):
            await asyncio.sleep(0.01)
        slot.begin_close()
        await slot.client.aclose()
        # Retired physical objects are not lifecycle history.  Only a proven
        # successful physical close may drop the heavyweight slot/client graph;
        # a close failure stays retained for Host shutdown diagnosis/join.
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
                    or candidate.expected_supervisor_epoch != self._epoch
                    or candidate.expected_semantic_config_fingerprint
                    != config.semantic_config_fingerprint
                    or candidate.expected_runtime_config_fingerprint
                    != config.runtime_config_fingerprint
                    or candidate.expected_resolved_config_identity
                    != config.resolved_config_identity
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
            specs = tuple(
                tool
                for item in candidates
                for tool in item.discovery_snapshot.tools
            )
            root = tuple(
                sorted(specs, key=lambda item: item.provider_tool_name)
            )
            if len({item.provider_tool_name for item in root}) != len(root):
                raise RuntimeError("MCP provider tool names collide across servers")
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
            catalog = self._catalog_locked()
        child = tuple(
            item for item in root if item.subagent_visible
        )
        policy_by_name = {
            policy.provider_tool_name: policy
            for candidate in candidates
            for policy in candidate.ordered_tool_execution_policies
        }
        bindings: list[PreparedToolExecutionBinding] = []
        executors: dict[str, McpBoundToolExecutor] = {}
        for semantic in root:
            policy = policy_by_name[semantic.provider_tool_name]
            lease = lease_by_server[semantic.server_id]
            executor_fp = context_fingerprint(
                "mcp-tool-executor-binding:v1",
                {
                    "provider_tool_name": semantic.provider_tool_name,
                    "descriptor_fingerprint": semantic.descriptor_fingerprint,
                    "policy_fingerprint": policy.policy_fingerprint,
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
            slot_leases=leases,
            slot_lease_by_server=lease_by_server,
            candidates={item.server_id: item for item in candidates},
        )

    def catalog_snapshot(self) -> McpCatalogSnapshot:
        with self._lock:
            return self._catalog_locked()

    def _catalog_locked(self) -> McpCatalogSnapshot:
        entries: list[McpServerCatalogEntry] = []
        for config in self.configs:
            candidate = self._installed.get(config.server_id)
            snapshot = candidate.discovery_snapshot if candidate is not None else None
            entries.append(
                McpServerCatalogEntry(
                    server_id=config.server_id,
                    display_name=config.display_name,
                    status=self._state[config.server_id],
                    required=config.required,
                    exposed_tool_count=len(snapshot.tools) if snapshot else 0,
                    discovered_tool_count=(
                        snapshot.discovered_tool_count if snapshot else 0
                    ),
                    resource_count=len(snapshot.resources) if snapshot else 0,
                    resource_template_count=(len(snapshot.resource_templates) if snapshot else 0),
                    prompt_count=len(snapshot.prompts) if snapshot else 0,
                    bounded_tool_name_overview=(
                        tuple(item.provider_tool_name for item in snapshot.tools[:32])
                        if snapshot
                        else ()
                    ),
                    sanitized_instructions=(snapshot.sanitized_instructions if snapshot else ""),
                    stable_failure_category=self._failure.get(config.server_id),
                    tool_surface_semantic_fingerprint=(
                        snapshot.tool_surface_semantic_fingerprint if snapshot else None
                    ),
                    catalog_semantic_fingerprint=(
                        snapshot.catalog_semantic_fingerprint
                        if snapshot
                        else context_fingerprint(
                            "mcp-catalog-empty-server:v1",
                            (config.server_id, self._state[config.server_id].value),
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
        )

    def reconnect(self, server_id: str) -> None:
        active_connect: asyncio.Task[None] | None = None
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
            if self._closed:
                return
            self._closed = True
            tasks = tuple(self._tasks.values()) + tuple(self._refresh_tasks.values())
            tasks += tuple(self._periodic_tasks.values())
            tasks += tuple(self._retry_tasks.values())
            slot_close_tasks = tuple(self._slot_close_tasks)
            slots = tuple(self._all_slots)
            pending = tuple(self._pending.values())
            installed = tuple(self._installed.values())
            self._pending.clear()
            self._installed.clear()
            for slot in slots:
                slot.begin_close()
        for candidate in pending:
            candidate.slot_lease.release()
        for candidate in installed:
            candidate.slot_lease.release()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Terminate exact physical transports first.  Product-operation tasks
        # then leave their lane permits; runtime generations still retain only
        # opaque leases and are released by DirectKernelToolPort.
        await asyncio.gather(
            *(slot.client.aclose() for slot in slots), return_exceptions=True
        )
        while any(
            slot.active_slot_lease_count
            or slot.active_dispatch_count
            or slot.active_operation_count
            for slot in slots
        ):
            await asyncio.sleep(0.01)
        # Retiring-slot close tasks are physical owners, not retry timers.  They
        # must be joined rather than cancelled, even when Host close races them.
        if slot_close_tasks:
            await asyncio.gather(*slot_close_tasks, return_exceptions=True)


async def _discover(
    client: BoundedMcpSdkClient,
    config: McpServerConfig,
) -> tuple[McpDiscoverySnapshot, tuple[McpToolExecutionPolicyFact, ...]]:
    budget = _DiscoveryBudget(
        remaining_items=MAXIMUM_DISCOVERY_ITEMS_PER_SERVER,
        remaining_bytes=(
            DEFAULT_MCP_WIRE_BOUNDS.maximum_discovery_candidate_bytes_per_server
        ),
    )
    advertised = client.advertised_capabilities
    if not isinstance(advertised, McpAdvertisedCapabilities):
        raise McpProtocolConformanceError("MCP_SERVER_CAPABILITIES_MISSING")
    tools = (
        await _list_pages(client, client.session.list_tools, "tools", budget=budget)
        if advertised.tools
        else ()
    )
    resources = (
        await _list_pages(
            client, client.session.list_resources, "resources", budget=budget
        )
        if advertised.resources
        else ()
    )
    templates = (
        await _list_pages(
            client,
            client.session.list_resource_templates,
            "resource_templates",
            budget=budget,
        )
        if advertised.resources
        else ()
    )
    prompts = (
        await _list_pages(
            client, client.session.list_prompts, "prompts", budget=budget
        )
        if advertised.prompts
        else ()
    )
    if len(tools) > MAXIMUM_DISCOVERED_TOOLS_PER_SERVER:
        raise ValueError("MCP discovered tool bound exceeded")
    if len(tools) + len(resources) + len(templates) + len(prompts) > MAXIMUM_DISCOVERY_ITEMS_PER_SERVER:
        raise ValueError("MCP discovery item bound exceeded")
    remote_names = tuple(item.name for item in tools)
    names = mangle_mcp_tool_names(config.server_id, remote_names)
    include = config.exposure_policy.include_tool_names
    excluded = set(config.exposure_policy.exclude_tool_names)
    selected = tuple(
        item
        for item in tools
        if (include is None or item.name in include) and item.name not in excluded
    )
    unknown_effect = set(dict(config.effect_policy.tool_effect_overrides)) - set(remote_names)
    unknown_timeout = set(dict(config.per_tool_timeout_ms)) - set(remote_names)
    unknown_include = (set(include) - set(remote_names)) if include is not None else set()
    if unknown_effect or unknown_timeout or unknown_include:
        raise ValueError("MCP config references an unknown remote tool")
    semantics: list[McpToolSemanticFact] = []
    policies: list[McpToolExecutionPolicyFact] = []
    invalid_tool_count = 0
    for item in selected:
        try:
            validate_schema(item.input_schema, DEFAULT_MCP_WIRE_BOUNDS)
            if item.input_schema.get("type") != "object":
                raise ValueError("MCP tool input schema is not an object")
            _reject_external_refs(item.input_schema)
            input_dialect = _mcp_schema_dialect(item.input_schema)
            input_validator = validators.validator_for(item.input_schema)
            input_validator.check_schema(item.input_schema)
            frozen = freeze_json(item.input_schema)
            if not isinstance(frozen, FrozenJsonObjectFact):
                raise TypeError("MCP tool schema did not freeze to an object")
            output_schema = None
            if item.output_schema is not None:
                validate_schema(item.output_schema, DEFAULT_MCP_WIRE_BOUNDS)
                _reject_external_refs(item.output_schema)
                _mcp_schema_dialect(item.output_schema)
                output_validator = validators.validator_for(item.output_schema)
                output_validator.check_schema(item.output_schema)
                output_schema = freeze_json(item.output_schema)
                if not isinstance(output_schema, FrozenJsonObjectFact):
                    raise TypeError("MCP output schema did not freeze to an object")
        except Exception:
            if (
                config.exposure_policy.invalid_tool_policy
                is McpInvalidToolPolicy.FAIL_SERVER
            ):
                raise
            invalid_tool_count += 1
            continue
        provider_name = names[item.name]
        public_description = _sanitize_text(item.description or "", 8 * 1024)
        descriptor = context_fingerprint(
            "mcp-tool-semantic:v1",
            {
                "server_id": config.server_id,
                "remote_tool_name": item.name,
                "provider_tool_name": provider_name,
                "description": public_description,
                "input_schema": frozen,
                "output_schema": output_schema,
                "schema_dialect": input_dialect,
                "scope_policy": config.scope_policy.value,
            },
        )
        semantic = McpToolSemanticFact(
            server_id=config.server_id,
            remote_tool_name=item.name,
            provider_tool_name=provider_name,
            description=public_description,
            input_schema=frozen,
            output_schema=output_schema,
            schema_dialect=input_dialect,
            descriptor_fingerprint=descriptor,
            root_visible=True,
            subagent_visible=(
                config.scope_policy is McpScopePolicy.ROOT_AND_SUBAGENTS
            ),
        )
        semantics.append(semantic)
        effect, source = _effect(config, item)
        timeout = dict(config.per_tool_timeout_ms).get(
            item.name, config.default_tool_timeout_ms
        )
        policy_fp = context_fingerprint(
            "mcp-tool-execution-policy:v1",
            {
                "server_id": config.server_id,
                "remote_tool_name": item.name,
                "provider_tool_name": provider_name,
                "tool_semantic_fingerprint": descriptor,
                "effect_kind": effect.value,
                "timeout_ms": timeout,
                "parallel_safe": config.supports_parallel_tool_calls,
                "classification_source": source.value,
            },
        )
        policies.append(
            McpToolExecutionPolicyFact(
                server_id=config.server_id,
                remote_tool_name=item.name,
                provider_tool_name=provider_name,
                tool_semantic_fingerprint=descriptor,
                effect_kind=effect,
                timeout_ms=timeout,
                parallel_safe=config.supports_parallel_tool_calls,
                classification_source=source,
                policy_fingerprint=policy_fp,
            )
        )
    semantic_tuple = tuple(sorted(semantics, key=lambda item: item.provider_tool_name))
    policy_tuple = tuple(sorted(policies, key=lambda item: item.provider_tool_name))
    resource_facts = tuple(
        _resource(config.server_id, item) for item in sorted(resources, key=lambda item: str(item.uri))
    )
    template_facts = tuple(
        _resource_template(config.server_id, item)
        for item in sorted(templates, key=lambda item: item.uri_template)
    )
    prompt_facts = tuple(
        _prompt(config.server_id, item) for item in sorted(prompts, key=lambda item: item.name)
    )
    instructions = _sanitize_text(client.server_instructions, MAXIMUM_MCP_INSTRUCTIONS_BYTES)
    tool_fp = context_fingerprint(
        "mcp-server-tool-surface:v1",
        tuple(item.descriptor_fingerprint for item in semantic_tuple),
    )
    catalog_fp = context_fingerprint(
        "mcp-server-catalog:v1",
        {
            "server_id": config.server_id,
            "display_name": config.display_name,
            "instructions": instructions,
            "tools": tuple(item.descriptor_fingerprint for item in semantic_tuple),
            "resources": tuple(item.semantic_fingerprint for item in resource_facts),
            "templates": tuple(item.semantic_fingerprint for item in template_facts),
            "prompts": tuple(item.semantic_fingerprint for item in prompt_facts),
        },
    )
    snapshot = McpDiscoverySnapshot(
        server_id=config.server_id,
        display_name=config.display_name,
        protocol_version=client.protocol_version,
        sanitized_instructions=instructions,
        discovered_tool_count=len(tools),
        invalid_tool_count=invalid_tool_count,
        tools=semantic_tuple,
        resources=resource_facts,
        resource_templates=template_facts,
        prompts=prompt_facts,
        tool_surface_semantic_fingerprint=tool_fp,
        catalog_semantic_fingerprint=catalog_fp,
        presentation_fingerprint=context_fingerprint(
            "mcp-server-presentation:v1",
            (config.server_id, config.display_name, len(semantic_tuple)),
        ),
        sdk_conformance_contract_fingerprint=context_fingerprint(
            "mcp-sdk-conformance:v1", ("mcp==2.0.0", "mcp-types==2.0.0")
        ),
    )
    if _discovery_snapshot_physical_bytes(snapshot) > (
        DEFAULT_MCP_WIRE_BOUNDS.maximum_discovery_candidate_bytes_per_server
    ):
        raise ValueError("MCP discovery candidate byte bound exceeded")
    return snapshot, policy_tuple


@dataclass(slots=True)
class _DiscoveryBudget:
    remaining_items: int
    remaining_bytes: int

    def charge(self, item: object) -> None:
        if self.remaining_items <= 0:
            raise ValueError("MCP discovery item bound exceeded")
        model_dump = getattr(item, "model_dump", None)
        if not callable(model_dump):
            raise ValueError("MCP discovery item carrier is invalid")
        physical_bytes = len(
            canonical_json_bytes(
                model_dump(by_alias=True, mode="json", exclude_none=True)
            )
        )
        if physical_bytes > self.remaining_bytes:
            raise ValueError("MCP discovery candidate byte bound exceeded")
        self.remaining_items -= 1
        self.remaining_bytes -= physical_bytes


async def _list_pages(
    client: BoundedMcpSdkClient,
    method: object,
    field_name: str,
    *,
    budget: _DiscoveryBudget,
) -> tuple[object, ...]:
    cursor: str | None = None
    items: list[object] = []
    seen: set[str] = set()
    for _ in range(MAXIMUM_DISCOVERY_PAGES_PER_METHOD):
        params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
        result = await method(params=params)  # type: ignore[operator]
        if client.require_closed_result_type(result) != "complete":
            raise RuntimeError("MCP listing cannot require input")
        page = getattr(result, field_name)
        if not isinstance(page, list):
            raise ValueError("MCP discovery page shape is invalid")
        for item in page:
            budget.charge(item)
        items.extend(page)
        cursor = result.next_cursor
        if cursor is None:
            return tuple(items)
        if cursor in seen or len(cursor.encode("utf-8")) > 4096:
            raise ValueError("MCP discovery cursor is invalid")
        seen.add(cursor)
    raise ValueError("MCP discovery page bound exceeded")


def _candidate(
    *,
    config: McpServerConfig,
    epoch: int,
    attempt_generation: int,
    lease: McpSlotLease,
    snapshot: McpDiscoverySnapshot,
    policies: tuple[McpToolExecutionPolicyFact, ...],
) -> McpInstallationCandidate:
    normalized_physical_bytes = _discovery_snapshot_physical_bytes(snapshot)
    fingerprint = context_fingerprint(
        "mcp-installation-candidate:v1",
        {
            "server_id": config.server_id,
            "epoch": epoch,
            "semantic_config": config.semantic_config_fingerprint,
            "runtime_config": config.runtime_config_fingerprint,
            "resolved_config": config.resolved_config_identity,
            "attempt_generation": attempt_generation,
            "slot_lease_identity": lease.lease_identity,
            "tool_surface": snapshot.tool_surface_semantic_fingerprint,
            "catalog": snapshot.catalog_semantic_fingerprint,
            "conformance": snapshot.sdk_conformance_contract_fingerprint,
            "policies": tuple(item.policy_fingerprint for item in policies),
            "standard_read_timeout_ms": config.default_tool_timeout_ms,
            "normalized_physical_bytes": normalized_physical_bytes,
        },
    )
    return McpInstallationCandidate(
        candidate_id=f"mcp-candidate:{fingerprint.removeprefix('sha256:')}",
        server_id=config.server_id,
        expected_supervisor_epoch=epoch,
        expected_semantic_config_fingerprint=config.semantic_config_fingerprint,
        expected_runtime_config_fingerprint=config.runtime_config_fingerprint,
        expected_resolved_config_identity=config.resolved_config_identity,
        attempt_generation=attempt_generation,
        slot_lease=lease,
        discovery_snapshot=snapshot,
        ordered_tool_execution_policies=policies,
        standard_read_timeout_ms=config.default_tool_timeout_ms,
        normalized_physical_bytes=normalized_physical_bytes,
        candidate_fingerprint=fingerprint,
    )


def _remaining_mcp_timeout(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError("MCP tool operation deadline expired")
    return remaining


def _effect(
    config: McpServerConfig, tool: types.Tool
) -> tuple[McpEffectKind, McpPolicyClassificationSource]:
    overrides = dict(config.effect_policy.tool_effect_overrides)
    if tool.name in overrides:
        return McpEffectKind(overrides[tool.name].value), McpPolicyClassificationSource.TOOL_OVERRIDE
    if config.effect_policy.default_effect is not McpConfiguredEffect.AUTO:
        return McpEffectKind(config.effect_policy.default_effect.value), McpPolicyClassificationSource.SERVER_OVERRIDE
    annotations = tool.annotations
    read_only = bool(annotations and annotations.read_only_hint is True)
    destructive = bool(annotations and annotations.destructive_hint is True)
    open_world = bool(annotations and annotations.open_world_hint is True)
    effect = (
        McpEffectKind.READ_ONLY
        if read_only and not destructive and not open_world
        else McpEffectKind.EXTERNAL_EFFECT
    )
    return effect, McpPolicyClassificationSource.SERVER_ANNOTATIONS


def _resource(server_id: str, item: types.Resource) -> McpResourceSemanticFact:
    payload = {
        "server_id": server_id,
        "uri": _bounded_public_identity(str(item.uri), 32 * 1024),
        "name": _bounded_public_identity(item.name, 8 * 1024),
        "description": _sanitize_text(item.description or "", 8 * 1024),
        "mime_type": (
            _bounded_public_identity(item.mime_type, 1024)
            if item.mime_type is not None
            else None
        ),
    }
    fact = McpResourceSemanticFact(
        **payload,
        semantic_fingerprint=context_fingerprint("mcp-resource:v1", payload),
    )
    _assert_catalog_item_bound(_resource_public_item(server_id, fact))
    return fact


def _resource_template(
    server_id: str, item: types.ResourceTemplate
) -> McpResourceTemplateSemanticFact:
    payload = {
        "server_id": server_id,
        "uri_template": _bounded_public_identity(item.uri_template, 32 * 1024),
        "name": _bounded_public_identity(item.name, 8 * 1024),
        "description": _sanitize_text(item.description or "", 8 * 1024),
        "mime_type": (
            _bounded_public_identity(item.mime_type, 1024)
            if item.mime_type is not None
            else None
        ),
    }
    fact = McpResourceTemplateSemanticFact(
        **payload,
        semantic_fingerprint=context_fingerprint("mcp-resource-template:v1", payload),
    )
    _assert_catalog_item_bound(_resource_template_public_item(server_id, fact))
    return fact


def _prompt(server_id: str, item: types.Prompt) -> McpPromptSemanticFact:
    arguments = tuple(
        (
            _bounded_public_identity(arg.name, 8 * 1024),
            _sanitize_text(arg.description or "", 8 * 1024),
            bool(arg.required),
        )
        for arg in (item.arguments or [])
    )
    payload = {
        "server_id": server_id,
        "name": _bounded_public_identity(item.name, 8 * 1024),
        "description": _sanitize_text(item.description or "", 8 * 1024),
        "arguments": arguments,
    }
    fact = McpPromptSemanticFact(
        **payload,
        semantic_fingerprint=context_fingerprint("mcp-prompt:v1", payload),
    )
    _assert_catalog_item_bound(_prompt_public_item(server_id, fact))
    return fact


def _resource_public_item(
    server_id: str, item: McpResourceSemanticFact
) -> dict[str, object]:
    return {
        "server_id": server_id,
        "uri": item.uri,
        "name": item.name,
        "description": item.description,
        "mime_type": item.mime_type,
    }


def _resource_template_public_item(
    server_id: str, item: McpResourceTemplateSemanticFact
) -> dict[str, object]:
    return {
        "server_id": server_id,
        "uri_template": item.uri_template,
        "name": item.name,
        "description": item.description,
        "mime_type": item.mime_type,
    }


def _prompt_public_item(
    server_id: str, item: McpPromptSemanticFact
) -> dict[str, object]:
    return {
        "server_id": server_id,
        "name": item.name,
        "description": item.description,
        "arguments": [
            {"name": name, "description": description, "required": required}
            for name, description, required in item.arguments
        ],
    }


def _assert_catalog_item_bound(item: object) -> None:
    if len(canonical_json_bytes({"items": [item]})) > (
        MAXIMUM_MCP_CATALOG_RESULT_BYTES
    ):
        raise ValueError("MCP catalog item exceeds the product result bound")


def _bounded_public_identity(value: str, maximum_bytes: int) -> str:
    if not value or len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError("MCP public identity exceeds its product bound")
    if any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        for character in value
    ):
        raise ValueError("MCP public identity contains terminal controls")
    return value


def _reject_external_refs(value: object) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            ref = item.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#"):
                raise ValueError("external MCP schema references are unsupported")
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def _mcp_schema_dialect(value: Mapping[str, object]) -> str:
    raw = value.get("$schema", _DEFAULT_MCP_SCHEMA_DIALECT)
    if not isinstance(raw, str) or raw not in _SUPPORTED_MCP_SCHEMA_DIALECTS:
        raise ValueError("unsupported MCP JSON Schema dialect")
    return raw


_URI_TEMPLATE_VARIABLE = re.compile(
    r"^[A-Za-z0-9_.%]+(?::[1-9][0-9]{0,3}|\*)?$"
)


def _resource_uri_in_snapshot(
    uri: str, snapshot: McpDiscoverySnapshot
) -> bool:
    """Join a resource URI to one exact static entry or RFC6570 template.

    This is an admission check, not a second resource authority: the template
    itself remains part of the immutable discovery generation and the remote
    server still decides whether the concrete resource exists.  Unsupported or
    malformed template syntax stays visible as catalog metadata but cannot
    authorize an open-ended read.
    """

    try:
        encoded = uri.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if not encoded or len(encoded) > 32 * 1024 or any(ord(char) < 0x20 for char in uri):
        return False
    if uri in {item.uri for item in snapshot.resources}:
        return True
    return any(
        _resource_uri_matches_template(uri, item.uri_template)
        for item in snapshot.resource_templates
    )


def _resource_uri_matches_template(uri: str, template: str) -> bool:
    """Linearly match a conservative, bounded RFC6570 subset.

    Regex wildcards are deliberately absent: each literal is consumed once and
    each expansion is validated once.  Adjacent expressions are rejected
    because their split is not uniquely provable without backtracking.  Named
    query and matrix expansions admit only their advertised variable names.
    """

    try:
        if (
            not uri
            or len(uri.encode("utf-8")) > 32 * 1024
            or len(template.encode("utf-8")) > 32 * 1024
        ):
            return False
    except UnicodeEncodeError:
        return False
    if any(ord(character) < 0x20 for character in uri + template):
        return False
    parsed = _parse_resource_uri_template(template)
    if parsed is None:
        return False
    expressions, tail = parsed
    position = 0
    for index, (literal, operator, variables) in enumerate(expressions):
        if not uri.startswith(literal, position):
            return False
        position += len(literal)
        next_literal = (
            expressions[index + 1][0]
            if index + 1 < len(expressions)
            else tail
        )
        if next_literal:
            end = uri.find(next_literal, position)
            if end < 0:
                return False
        else:
            end = len(uri)
        if not _match_uri_template_expansion(
            uri[position:end], operator=operator, variables=variables
        ):
            return False
        position = end
    return uri.startswith(tail, position) and position + len(tail) == len(uri)


def _parse_resource_uri_template(
    template: str,
) -> tuple[tuple[tuple[str, str, tuple[str, ...]], ...], str] | None:
    expressions: list[tuple[str, str, tuple[str, ...]]] = []
    cursor = 0
    while cursor < len(template):
        opening = template.find("{", cursor)
        closing_without_open = template.find("}", cursor)
        if opening < 0:
            if closing_without_open >= 0:
                return None
            break
        if closing_without_open >= 0 and closing_without_open < opening:
            return None
        literal = template[cursor:opening]
        # Without a literal separator there is no unique linear split between
        # two expansions, so the conservative admission answer is false.
        if expressions and not literal:
            return None
        closing = template.find("}", opening + 1)
        if closing < 0 or "{" in template[opening + 1 : closing]:
            return None
        expression = template[opening + 1 : closing]
        if not expression or len(expressions) >= 128:
            return None
        operator = expression[0] if expression[0] in "+#./;?&" else ""
        variable_list = expression[1:] if operator else expression
        variables = tuple(variable_list.split(","))
        if (
            not variables
            or len(variables) > 128
            or any(
                _URI_TEMPLATE_VARIABLE.fullmatch(variable) is None
                for variable in variables
            )
        ):
            return None
        expressions.append((literal, operator, variables))
        cursor = closing + 1
    tail = template[cursor:]
    if "{" in tail or "}" in tail:
        return None
    return tuple(expressions), tail


def _match_uri_template_expansion(
    expansion: str,
    *,
    operator: str,
    variables: tuple[str, ...],
) -> bool:
    names = tuple(_uri_template_variable_name(variable) for variable in variables)
    exploded = {
        _uri_template_variable_name(variable)
        for variable in variables
        if variable.endswith("*")
    }
    if operator in {"?", "&"}:
        return _match_named_uri_expansion(
            expansion,
            prefix=operator,
            separator="&",
            names=names,
            exploded=exploded,
        )
    if operator == ";":
        return _match_named_uri_expansion(
            expansion,
            prefix=";",
            separator=";",
            names=names,
            exploded=exploded,
        )
    if not expansion:
        return True
    if operator == "+":
        return "#" not in expansion
    if operator == "#":
        return expansion.startswith("#") and "#" not in expansion[1:]
    if operator in {".", "/"}:
        if not expansion.startswith(operator) or any(
            character in "?#" for character in expansion
        ):
            return False
        values = expansion[1:].split(operator)
        return all(values) and (
            len(values) <= len(variables) or bool(exploded)
        )
    if any(character in "?#" for character in expansion):
        return False
    if exploded:
        # A simple exploded variable is used by common resource templates for
        # a path tail.  It may cross '/', but never query or fragment syntax.
        return True
    if any(character in ":/" for character in expansion):
        return False
    values = expansion.split(",")
    return len(values) <= len(variables)


def _match_named_uri_expansion(
    expansion: str,
    *,
    prefix: str,
    separator: str,
    names: tuple[str, ...],
    exploded: set[str],
) -> bool:
    if not expansion:
        return True
    if not expansion.startswith(prefix) or "#" in expansion:
        return False
    fields = expansion[1:].split(separator)
    if not fields or any(not field for field in fields):
        return False
    seen: set[str] = set()
    allowed = set(names)
    for parameter in fields:
        name, _separator_found, _value = parameter.partition("=")
        if not name or name not in allowed:
            return False
        if name in seen and name not in exploded:
            return False
        # Matrix parameters may omit '=value'; query parameters may too.  In
        # both cases admission depends on the exact advertised key, not value.
        seen.add(name)
    return True


def _uri_template_variable_name(variable: str) -> str:
    raw = variable[:-1] if variable.endswith("*") else variable
    return raw.split(":", 1)[0]


def _sanitize_text(value: str, maximum_bytes: int) -> str:
    safe = "".join(
        character
        if character in {"\n", "\t"}
        or (ord(character) >= 0x20 and not 0x7F <= ord(character) <= 0x9F)
        else "�"
        for character in value
    )
    encoded = safe.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return safe
    return encoded[:maximum_bytes].decode("utf-8", "ignore")


def _discovery_snapshot_physical_bytes(snapshot: McpDiscoverySnapshot) -> int:
    """Quote the immutable normalized candidate before installation."""

    return len(
        canonical_json_bytes(
            {
                "server_id": snapshot.server_id,
                "display_name": snapshot.display_name,
                "protocol_version": snapshot.protocol_version,
                "instructions": snapshot.sanitized_instructions,
                "discovered_tool_count": snapshot.discovered_tool_count,
                "invalid_tool_count": snapshot.invalid_tool_count,
                "tools": tuple(
                    {
                        "server_id": item.server_id,
                        "remote_name": item.remote_tool_name,
                        "provider_name": item.provider_tool_name,
                        "description": item.description,
                        "input_schema": item.input_schema,
                        "output_schema": item.output_schema,
                        "descriptor": item.descriptor_fingerprint,
                    }
                    for item in snapshot.tools
                ),
                "resources": tuple(
                    (
                        item.server_id,
                        item.uri,
                        item.name,
                        item.description,
                        item.mime_type,
                        item.semantic_fingerprint,
                    )
                    for item in snapshot.resources
                ),
                "resource_templates": tuple(
                    (
                        item.server_id,
                        item.uri_template,
                        item.name,
                        item.description,
                        item.mime_type,
                        item.semantic_fingerprint,
                    )
                    for item in snapshot.resource_templates
                ),
                "prompts": tuple(
                    (
                        item.server_id,
                        item.name,
                        item.description,
                        item.arguments,
                        item.semantic_fingerprint,
                    )
                    for item in snapshot.prompts
                ),
                "tool_surface": snapshot.tool_surface_semantic_fingerprint,
                "catalog": snapshot.catalog_semantic_fingerprint,
                "conformance": snapshot.sdk_conformance_contract_fingerprint,
            }
        )
    )


def _render_typed_result(result: types.CallToolResult) -> bytes:
    payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    payload.pop("_meta", None)
    data = canonical_json_bytes(payload)
    if len(data) > MAXIMUM_MCP_REMOTE_BODY_BYTES:
        raise RuntimeError("MCP_REMOTE_BODY_BOUND_EXCEEDED")
    return data


def _validate_tool_output_schema(
    semantic: McpToolSemanticFact,
    result: types.CallToolResult,
) -> None:
    """Validate an exact response against the descriptor from its pinned slot.

    This is deliberately performed after the physical response is known and
    before result lowering.  A mismatch is therefore a known typed tool
    failure, never an ambiguous effect and never a reason to call the server
    again.
    """

    if semantic.output_schema is None:
        return
    if (
        "structured_content" not in result.model_fields_set
        or result.structured_content is None
    ):
        raise _McpOutputSchemaMismatch("structured MCP result is absent")
    schema = thaw_json(semantic.output_schema)
    _mcp_schema_dialect(schema)
    validator_type = validators.validator_for(schema)
    try:
        validator_type.check_schema(schema)
        validator = validator_type(schema)
        error = next(validator.iter_errors(result.structured_content), None)
    except BaseException as exc:
        raise _McpOutputSchemaMismatch("MCP output schema cannot validate") from exc
    if error is not None:
        raise _McpOutputSchemaMismatch("MCP structured result does not match schema")


def _mcp_remote_identity(
    *, semantic: McpToolSemanticFact, connection_generation: int
) -> str:
    # Remote tool names are server-controlled and the MCP SDK intentionally
    # does not impose Pulsara's 4 KiB canonical identity bound.  Freeze the
    # complete exact identity into a domain-separated digest before physical
    # dispatch, so every exposed tool can settle a known result regardless of
    # the display-name length.
    return "mcp-tool:" + context_fingerprint(
        "pulsara:mcp-remote-tool-identity:v1",
        {
            "server_id": semantic.server_id,
            "connection_generation": connection_generation,
            "remote_tool_name": semantic.remote_tool_name,
        },
    )


def _known_mcp_system_failure(
    *, error_code: str, remote_identity: str
) -> McpKnownToolResult:
    return McpKnownToolResult(
        state="SYSTEM_ERROR",
        content=canonical_json_bytes(
            {
                "error": error_code,
                "retry_performed": False,
            }
        ),
        remote_identity=remote_identity,
    )


def _local_known(payload: object, tool_name: str) -> McpKnownToolResult:
    body = canonical_json_bytes(payload)
    if len(body) > MAXIMUM_MCP_CATALOG_RESULT_BYTES:
        raise RuntimeError("MCP catalog result exceeds the product bound")
    return McpKnownToolResult(
        state="SUCCESS",
        content=body,
        remote_identity=f"mcp-catalog:{tool_name}",
    )


def _server_catalog_item(item: McpServerCatalogEntry) -> dict[str, object]:
    return {
        "server_id": item.server_id,
        "display_name": item.display_name,
        "status": item.status.value,
        "required": item.required,
        "exposed_tool_count": item.exposed_tool_count,
        "resource_count": item.resource_count,
        "resource_template_count": item.resource_template_count,
        "prompt_count": item.prompt_count,
        "tool_names": item.bounded_tool_name_overview,
        "instructions": item.sanitized_instructions,
        "failure_category": item.stable_failure_category,
    }


def _bounded_server_catalog_payload(catalog: McpCatalogSnapshot) -> object:
    servers: list[dict[str, object]] = []
    total = len(catalog.servers)
    for item in catalog.servers:
        candidate = [*servers, _server_catalog_item(item)]
        payload = {
            "servers": candidate,
            "omitted_server_count": total - len(candidate),
        }
        if len(canonical_json_bytes(payload)) > MAXIMUM_MCP_CATALOG_RESULT_BYTES:
            break
        servers = candidate
    return {
        "servers": servers,
        "omitted_server_count": total - len(servers),
    }


def _list_catalog_items(
    runtime: McpInstalledRuntimeGeneration,
    tool_name: str,
    arguments: Mapping[str, object],
    scope_kind: ModelInputScopeKind,
) -> object:
    visible = {
        item.server_id
        for item in runtime.catalog_snapshot.for_scope(scope_kind).servers
    }
    server_filter = arguments.get("server_id")
    if server_filter is not None:
        if not isinstance(server_filter, str) or server_filter not in visible:
            raise ValueError("MCP server is not scope-visible")
        visible = {server_filter}
    limit = arguments.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("MCP catalog list limit is invalid")
    cursor = arguments.get("cursor")
    offset = 0
    if cursor is not None:
        prefix = f"catalog:{runtime.catalog_snapshot.semantic_fingerprint}:offset:"
        if not isinstance(cursor, str) or not cursor.startswith(prefix):
            raise ValueError("MCP catalog cursor is invalid")
        try:
            offset = int(cursor.removeprefix(prefix))
        except ValueError as exc:
            raise ValueError("MCP catalog cursor is invalid") from exc
        if offset < 0:
            raise ValueError("MCP catalog cursor is invalid")
    def iter_items():
        for server_id in sorted(visible):
            candidate = runtime.candidates.get(server_id)
            if candidate is None:
                continue
            snapshot = candidate.discovery_snapshot
            if tool_name == "list_mcp_resources":
                for item in snapshot.resources:
                    yield _resource_public_item(server_id, item)
            elif tool_name == "list_mcp_resource_templates":
                for item in snapshot.resource_templates:
                    yield _resource_template_public_item(server_id, item)
            else:
                for item in snapshot.prompts:
                    yield _prompt_public_item(server_id, item)

    total = sum(1 for _ in iter_items())
    page: list[dict[str, object]] = []
    for index, item in enumerate(iter_items()):
        if index < offset:
            continue
        if len(page) >= limit:
            break
        candidate_page = [*page, item]
        candidate_offset = offset + len(candidate_page)
        candidate_payload = {
            "items": candidate_page,
            "next_cursor": (
                "catalog:"
                f"{runtime.catalog_snapshot.semantic_fingerprint}:offset:"
                f"{candidate_offset}"
                if candidate_offset < total
                else None
            ),
            "total": total,
        }
        if len(canonical_json_bytes(candidate_payload)) > (
            MAXIMUM_MCP_CATALOG_RESULT_BYTES
        ):
            break
        page = candidate_page
    if offset < total and not page:
        raise RuntimeError("MCP catalog item exceeds the product result bound")
    next_offset = offset + len(page)
    return {
        "items": page,
        "next_cursor": (
            "catalog:"
            f"{runtime.catalog_snapshot.semantic_fingerprint}:offset:{next_offset}"
            if next_offset < total
            else None
        ),
        "total": total,
    }


__all__ = [
    "McpBoundToolExecutor",
    "McpDispatchAdmissionPermit",
    "McpHostSupervisor",
    "McpInstalledRuntimeGeneration",
    "McpKnownToolResult",
    "McpPhysicalOutcomeUnknown",
    "McpSlotLease",
    "McpSnapshotStale",
]
