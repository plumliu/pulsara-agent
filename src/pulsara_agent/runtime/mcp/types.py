"""Typed MCP configuration and discovery DTOs.

These types are deliberately small and Pulsara-owned.  They model the MCP
surface we need for capability exposure and execution without importing an MCP
SDK into the runtime core.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from urllib.parse import SplitResult, urlsplit, urlunsplit
from typing import Any, Mapping
from uuid import uuid4

from pulsara_agent.primitives.mcp import (
    freeze_mcp_json_value,
    McpDiagnosticFact,
    McpInstalledServerSnapshotFact,
    McpReconcileAttemptSummaryFact,
    McpReconcileTriggerValue,
    McpServerLifecycleTimingFact,
)


class McpServerTransportKind(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class McpServerStatus(StrEnum):
    DISABLED = "disabled"
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    NEEDS_AUTH = "needs_auth"
    DEGRADED = "degraded"
    CLOSING = "closing"
    CLOSED = "closed"


class McpRequestSourceMethod(StrEnum):
    TOOL_CALL = "tools/call"
    RESOURCE_READ = "resources/read"
    PROMPT_GET = "prompts/get"


MAX_MCP_INPUT_REQUIRED_ROUNDS = 3
MAX_MCP_SERVERS_PER_SESSION = 64


@dataclass(frozen=True, slots=True)
class McpStdioConfig:
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    cwd: Path | None = None

    def __post_init__(self) -> None:
        if not self.command.strip():
            raise ValueError("MCP stdio command is required")
        object.__setattr__(self, "args", tuple(str(arg) for arg in self.args))
        object.__setattr__(self, "env", MappingProxyType({str(k): str(v) for k, v in self.env.items()}))
        if self.cwd is not None:
            object.__setattr__(self, "cwd", self.cwd.expanduser())


@dataclass(frozen=True, slots=True)
class McpStreamableHttpConfig:
    url: str
    bearer_token_env_var: str | None = None
    headers: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    env_headers: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    follow_redirects: bool = False

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("MCP streamable HTTP URL is required")
        object.__setattr__(self, "url", self.url.strip())
        if not (self.url.startswith("http://") or self.url.startswith("https://")):
            raise ValueError("MCP streamable HTTP URL must use http:// or https://")
        object.__setattr__(self, "headers", MappingProxyType({str(k): str(v) for k, v in self.headers.items()}))
        object.__setattr__(
            self,
            "env_headers",
            MappingProxyType({str(k): str(v) for k, v in self.env_headers.items()}),
        )
        if self.bearer_token_env_var is not None and not self.bearer_token_env_var.strip():
            raise ValueError("bearer_token_env_var cannot be empty")


McpTransportConfig = McpStdioConfig | McpStreamableHttpConfig


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    server_id: str
    transport: McpTransportConfig
    enabled: bool = True
    required: bool = False
    connect_timeout_ms: int = 10_000
    discovery_timeout_ms: int = 15_000
    startup_deadline_ms: int = 30_000
    refresh_ttl_ms: int = 300_000
    tool_timeout_ms: int = 30_000
    supports_parallel_tool_calls: bool = False
    enabled_tools: tuple[str, ...] | None = None
    disabled_tools: tuple[str, ...] = ()
    default_approval_mode: str | None = None

    def __post_init__(self) -> None:
        normalized_id = normalize_mcp_identifier(self.server_id)
        if not normalized_id:
            raise ValueError("MCP server_id is required")
        timeout_values = {
            "connect_timeout_ms": self.connect_timeout_ms,
            "discovery_timeout_ms": self.discovery_timeout_ms,
            "startup_deadline_ms": self.startup_deadline_ms,
            "refresh_ttl_ms": self.refresh_ttl_ms,
            "tool_timeout_ms": self.tool_timeout_ms,
        }
        for name, value in timeout_values.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.startup_deadline_ms < self.connect_timeout_ms:
            raise ValueError("startup_deadline_ms must be >= connect_timeout_ms")
        object.__setattr__(self, "server_id", normalized_id)
        if self.enabled_tools is not None:
            object.__setattr__(self, "enabled_tools", tuple(dict.fromkeys(self.enabled_tools)))
        object.__setattr__(self, "disabled_tools", tuple(dict.fromkeys(self.disabled_tools)))

    @property
    def transport_kind(self) -> McpServerTransportKind:
        if isinstance(self.transport, McpStdioConfig):
            return McpServerTransportKind.STDIO
        return McpServerTransportKind.STREAMABLE_HTTP


@dataclass(frozen=True, slots=True)
class McpToolAnnotations:
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None
    open_world_hint: bool | None = None
    title: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "McpToolAnnotations":
        if not payload:
            return cls()
        return cls(
            read_only_hint=_optional_bool(payload.get("readOnlyHint") or payload.get("read_only_hint")),
            destructive_hint=_optional_bool(payload.get("destructiveHint") or payload.get("destructive_hint")),
            open_world_hint=_optional_bool(payload.get("openWorldHint") or payload.get("open_world_hint")),
            title=_optional_str(payload.get("title")),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "read_only_hint": self.read_only_hint,
            "destructive_hint": self.destructive_hint,
            "open_world_hint": self.open_world_hint,
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class McpDiscoveredTool:
    server_id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: McpToolAnnotations = field(default_factory=McpToolAnnotations)

    def __post_init__(self) -> None:
        if not self.server_id.strip():
            raise ValueError("MCP discovered tool requires server_id")
        if not self.name.strip():
            raise ValueError("MCP discovered tool requires name")
        object.__setattr__(self, "server_id", normalize_mcp_identifier(self.server_id))
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "description", self.description.strip() or f"MCP tool {self.name}")
        schema = dict(self.input_schema or {})
        if schema.get("type") != "object":
            schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        object.__setattr__(self, "input_schema", freeze_mcp_json_value(schema))


@dataclass(frozen=True, slots=True)
class McpDiscoveredResource:
    server_id: str
    uri: str
    name: str
    description: str = ""
    mime_type: str | None = None
    size: int | None = None

    def __post_init__(self) -> None:
        if not self.server_id.strip():
            raise ValueError("MCP discovered resource requires server_id")
        if not self.uri.strip():
            raise ValueError("MCP discovered resource requires uri")
        object.__setattr__(self, "server_id", normalize_mcp_identifier(self.server_id))
        object.__setattr__(self, "uri", self.uri.strip())
        object.__setattr__(self, "name", self.name.strip() or self.uri.strip())
        object.__setattr__(self, "description", self.description.strip())


@dataclass(frozen=True, slots=True)
class McpDiscoveredResourceTemplate:
    server_id: str
    uri_template: str
    name: str
    description: str = ""
    mime_type: str | None = None

    def __post_init__(self) -> None:
        if not self.server_id.strip():
            raise ValueError("MCP discovered resource template requires server_id")
        if not self.uri_template.strip():
            raise ValueError("MCP discovered resource template requires uri_template")
        object.__setattr__(self, "server_id", normalize_mcp_identifier(self.server_id))
        object.__setattr__(self, "uri_template", self.uri_template.strip())
        object.__setattr__(self, "name", self.name.strip() or self.uri_template.strip())
        object.__setattr__(self, "description", self.description.strip())


@dataclass(frozen=True, slots=True)
class McpDiscoveredPrompt:
    server_id: str
    name: str
    description: str = ""
    arguments: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.server_id.strip():
            raise ValueError("MCP discovered prompt requires server_id")
        if not self.name.strip():
            raise ValueError("MCP discovered prompt requires name")
        object.__setattr__(self, "server_id", normalize_mcp_identifier(self.server_id))
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "description", self.description.strip())
        object.__setattr__(
            self,
            "arguments",
            tuple(freeze_mcp_json_value(argument) for argument in self.arguments),
        )


@dataclass(frozen=True, slots=True)
class McpServerSnapshot:
    snapshot_id: str
    server_id: str
    config_epoch: int
    event_safe_config_fingerprint: str
    snapshot_semantic_fingerprint: str
    reconcile_attempt_id: str
    discovery_generation: int
    status: McpServerStatus
    required: bool
    tools: tuple[McpDiscoveredTool, ...] = ()
    resources: tuple[McpDiscoveredResource, ...] = ()
    resource_templates: tuple[McpDiscoveredResourceTemplate, ...] = ()
    prompts: tuple[McpDiscoveredPrompt, ...] = ()
    message: str | None = None
    protocol_version: str | None = None
    server_info: dict[str, Any] = field(default_factory=dict)
    instructions: str | None = None
    diagnostics: tuple[dict[str, Any], ...] = ()
    timing: McpServerLifecycleTimingFact | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "resources", tuple(self.resources))
        object.__setattr__(self, "resource_templates", tuple(self.resource_templates))
        object.__setattr__(self, "prompts", tuple(self.prompts))
        object.__setattr__(self, "server_info", freeze_mcp_json_value(self.server_info))
        object.__setattr__(
            self,
            "diagnostics",
            tuple(freeze_mcp_json_value(item) for item in self.diagnostics),
        )

        if self.config_epoch < 0 or self.discovery_generation < 0:
            raise ValueError("MCP snapshot generations must be non-negative")
        if self.status is not McpServerStatus.READY and (
            self.tools or self.resources or self.resource_templates or self.prompts
        ):
            raise ValueError("non-ready MCP snapshots cannot expose discovered collections")
        discovered_items = (
            *self.tools,
            *self.resources,
            *self.resource_templates,
            *self.prompts,
        )
        if any(item.server_id != self.server_id for item in discovered_items):
            raise ValueError("MCP snapshot discovered item server_id mismatch")
        if self.timing is None:
            raise ValueError("MCP snapshot requires lifecycle timing")
        if self.status is not McpServerStatus.STARTING and (
            self.timing.completed_at_utc is None
            or self.timing.total_duration_seconds is None
        ):
            raise ValueError("terminal MCP snapshot requires completed timing")
        if self.status is McpServerStatus.READY and (
            self.timing.connect_ended_at_utc is None
            or self.timing.discovery_ended_at_utc is None
        ):
            raise ValueError("ready MCP snapshot requires completed connect/discovery timing")


@dataclass(frozen=True, slots=True)
class McpRuntimeConfigIdentity:
    config_epoch: int
    runtime_config_set_fingerprint: str
    runtime_server_config_fingerprints: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class McpServerRuntimeSpec:
    config: McpServerConfig
    runtime_config_fingerprint: str
    event_safe_config_fingerprint: str


@dataclass(frozen=True, slots=True)
class McpServerAttempt:
    server_id: str
    reconcile_attempt_id: str
    config_epoch: int
    reserved_discovery_generation: int
    runtime_config_fingerprint: str
    deadline_monotonic: float

    def __post_init__(self) -> None:
        if not self.server_id or not self.reconcile_attempt_id:
            raise ValueError("MCP attempt identity fields are required")
        if self.config_epoch < 0 or self.reserved_discovery_generation < 0:
            raise ValueError("MCP attempt generations must be non-negative")
        if not math.isfinite(self.deadline_monotonic) or self.deadline_monotonic <= 0:
            raise ValueError("MCP attempt deadline must be finite and positive")


@dataclass(frozen=True, slots=True)
class McpReconcileTicket:
    ticket_id: str
    config_epoch: int
    event_safe_config_set_fingerprint: str
    trigger: McpReconcileTriggerValue
    required_server_ids: tuple[str, ...]
    optional_server_ids: tuple[str, ...]
    server_attempts: Mapping[str, McpServerAttempt]
    required_wait_deadline_monotonic: float | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_server_ids", tuple(self.required_server_ids))
        object.__setattr__(self, "optional_server_ids", tuple(self.optional_server_ids))
        object.__setattr__(
            self,
            "server_attempts",
            MappingProxyType(dict(self.server_attempts)),
        )
        if self.config_epoch < 0:
            raise ValueError("MCP ticket config epoch must be non-negative")
        if self.trigger not in {
            "initial",
            "config_change",
            "ttl_refresh",
            "retry",
            "manual_refresh",
        }:
            raise ValueError("invalid MCP ticket trigger")
        required = set(self.required_server_ids)
        optional = set(self.optional_server_ids)
        if len(required) != len(self.required_server_ids) or len(optional) != len(
            self.optional_server_ids
        ):
            raise ValueError("MCP ticket server ids must be unique")
        if required.intersection(optional):
            raise ValueError("MCP ticket required/optional server ids must be disjoint")
        desired = required | optional
        if any(
            key != attempt.server_id or key not in desired
            for key, attempt in self.server_attempts.items()
        ):
            raise ValueError("MCP ticket attempt attribution mismatch")
        expected_deadline = max(
            (
                attempt.deadline_monotonic
                for server_id, attempt in self.server_attempts.items()
                if server_id in required
            ),
            default=None,
        )
        if self.required_wait_deadline_monotonic != expected_deadline:
            raise ValueError("MCP ticket required wait deadline mismatch")


@dataclass(frozen=True, slots=True)
class McpBindingIdentity:
    server_id: str
    slot_id: str
    snapshot_id: str
    discovery_generation: int

    def __post_init__(self) -> None:
        if not self.server_id or not self.slot_id or not self.snapshot_id:
            raise ValueError("MCP binding identity fields are required")
        if self.discovery_generation < 0:
            raise ValueError("MCP binding generation must be non-negative")


@dataclass(slots=True)
class McpManagerSlot:
    slot_id: str
    server_id: str
    config_epoch: int
    runtime_config_fingerprint: str
    snapshot_id: str
    discovery_generation: int
    manager: Any
    lifecycle: str = "candidate"
    borrower_count: int = 0

    def __post_init__(self) -> None:
        if not self.slot_id or not self.server_id or not self.snapshot_id:
            raise ValueError("MCP manager slot identity fields are required")
        if self.config_epoch < 0 or self.discovery_generation < 0:
            raise ValueError("MCP manager slot generations must be non-negative")
        if self.lifecycle not in {
            "candidate",
            "installed",
            "retiring",
            "closing",
            "closed",
        }:
            raise ValueError("invalid MCP manager slot lifecycle")
        if self.borrower_count < 0:
            raise ValueError("MCP manager slot borrower count must be non-negative")

    @property
    def binding_identity(self) -> McpBindingIdentity:
        return McpBindingIdentity(
            server_id=self.server_id,
            slot_id=self.slot_id,
            snapshot_id=self.snapshot_id,
            discovery_generation=self.discovery_generation,
        )


@dataclass(slots=True)
class McpServerCandidate:
    ticket_id: str
    config_epoch: int
    reconcile_attempt_id: str
    reserved_discovery_generation: int
    server_snapshot: McpServerSnapshot
    runtime_spec: McpServerRuntimeSpec
    manager_slot: McpManagerSlot | None
    trigger: McpReconcileTriggerValue
    retry_attempt: int = 0
    request_count: int = 0
    page_count: int = 0
    cache_outcome: str = "miss"

    def __post_init__(self) -> None:
        snapshot = self.server_snapshot
        spec = self.runtime_spec
        if self.trigger not in {
            "initial",
            "config_change",
            "ttl_refresh",
            "retry",
            "manual_refresh",
        }:
            raise ValueError("invalid MCP candidate trigger")
        if self.config_epoch != snapshot.config_epoch:
            raise ValueError("MCP candidate config epoch mismatch")
        if self.reconcile_attempt_id != snapshot.reconcile_attempt_id:
            raise ValueError("MCP candidate attempt identity mismatch")
        if self.reserved_discovery_generation != snapshot.discovery_generation:
            raise ValueError("MCP candidate discovery generation mismatch")
        if spec.config.server_id != snapshot.server_id:
            raise ValueError("MCP candidate server identity mismatch")
        if (
            spec.event_safe_config_fingerprint
            != snapshot.event_safe_config_fingerprint
        ):
            raise ValueError("MCP candidate event-safe config identity mismatch")
        counts = (self.retry_attempt, self.request_count, self.page_count)
        if any(value < 0 for value in counts) or self.page_count > self.request_count:
            raise ValueError("MCP candidate request/page counts are invalid")
        if self.cache_outcome not in {
            "not_applicable",
            "miss",
            "ttl_fresh_reuse",
            "config_fingerprint_reuse",
            "sdk_response_cache_hit",
        }:
            raise ValueError("invalid MCP candidate cache outcome")
        if self.cache_outcome in {
            "ttl_fresh_reuse",
            "config_fingerprint_reuse",
            "sdk_response_cache_hit",
        } and (self.request_count or self.page_count):
            raise ValueError("MCP cached candidate cannot report network counts")
        slot = self.manager_slot
        if snapshot.status is McpServerStatus.READY:
            if slot is None:
                raise ValueError("ready MCP candidate requires manager slot")
        elif slot is not None:
            raise ValueError("non-ready MCP candidate cannot carry manager slot")
        if slot is not None and (
            slot.server_id != snapshot.server_id
            or slot.config_epoch != snapshot.config_epoch
            or slot.runtime_config_fingerprint != spec.runtime_config_fingerprint
            or slot.snapshot_id != snapshot.snapshot_id
            or slot.discovery_generation != snapshot.discovery_generation
            or slot.lifecycle != "candidate"
        ):
            raise ValueError("MCP candidate manager slot identity mismatch")


@dataclass(frozen=True, slots=True)
class McpCandidateBatch:
    config_epoch: int
    candidates: tuple[McpServerCandidate, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        if self.config_epoch < 0 or any(
            candidate.config_epoch != self.config_epoch
            for candidate in self.candidates
        ):
            raise ValueError("MCP candidate batch epoch mismatch")


@dataclass(frozen=True, slots=True)
class McpManagerLease:
    lease_id: str
    slot_id: str
    binding_identity: McpBindingIdentity

    def __post_init__(self) -> None:
        if not self.lease_id or self.slot_id != self.binding_identity.slot_id:
            raise ValueError("MCP manager lease identity mismatch")


@dataclass(frozen=True, slots=True)
class McpPendingLeaseReservation:
    reservation_id: str
    interaction_id: str
    binding_identity: McpBindingIdentity

    def __post_init__(self) -> None:
        if not self.reservation_id or not self.interaction_id:
            raise ValueError("MCP pending lease reservation identity is required")


@dataclass(slots=True)
class McpPendingLeaseOwner:
    interaction_id: str
    lease: McpManagerLease
    reservation_id: str
    confirmed: bool = False
    active_borrows: int = 0


@dataclass(frozen=True, slots=True)
class McpInstalledCapabilitySnapshot:
    installation_id: str
    config_epoch: int
    event_safe_config_set_fingerprint: str
    installed_at_utc: str
    snapshots: tuple[McpServerSnapshot, ...]
    descriptors: tuple[object, ...]
    tools: tuple[object, ...]
    diagnostics: tuple[object, ...]
    ready_server_ids: frozenset[str]
    binding_identities: frozenset[McpBindingIdentity]

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshots", tuple(self.snapshots))
        object.__setattr__(self, "descriptors", tuple(self.descriptors))
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(self, "ready_server_ids", frozenset(self.ready_server_ids))
        object.__setattr__(
            self,
            "binding_identities",
            frozenset(self.binding_identities),
        )
        if self.config_epoch < 0:
            raise ValueError("MCP installation config epoch must be non-negative")
        server_ids = tuple(snapshot.server_id for snapshot in self.snapshots)
        if len(set(server_ids)) != len(server_ids):
            raise ValueError("MCP installation server snapshots must be unique")
        expected_ready = frozenset(
            snapshot.server_id
            for snapshot in self.snapshots
            if snapshot.status is McpServerStatus.READY
        )
        if self.ready_server_ids != expected_ready:
            raise ValueError("MCP installation ready server projection mismatch")
        descriptor_names = {
            str(getattr(descriptor, "name", "")) for descriptor in self.descriptors
        }
        tool_names = {str(getattr(tool, "name", "")) for tool in self.tools}
        if descriptor_names != tool_names:
            raise ValueError("MCP installation descriptor/tool name mismatch")
        tool_identities = frozenset(
            identity
            for identity in (
                getattr(tool, "binding_identity", None) for tool in self.tools
            )
            if isinstance(identity, McpBindingIdentity)
        )
        if tool_identities != self.binding_identities:
            raise ValueError("MCP installation binding identity projection mismatch")


@dataclass(frozen=True, slots=True)
class McpPendingInstallationAudit:
    event_id: str
    installation_id: str
    previous_installation_id: str | None
    config_epoch: int
    event_safe_config_set_fingerprint: str
    installation_triggers: tuple[McpReconcileTriggerValue, ...]
    coalesced_installation_count: int
    coalesced_attempt_summaries: tuple[McpReconcileAttemptSummaryFact, ...]
    coalesced_attempt_summaries_omitted: int
    server_snapshots: tuple[McpInstalledServerSnapshotFact, ...]
    total_installed_tool_count: int
    added_tool_count: int
    revoked_tool_count: int
    changed_tool_names_bounded: tuple[str, ...]
    changed_tool_names_omitted: int
    diagnostics: tuple[McpDiagnosticFact, ...]
    baseline_tool_names: frozenset[str]
    current_tool_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class McpCapabilityExecutionSurface:
    installation: McpInstalledCapabilitySnapshot
    capability_runtime: object
    extra_tool_bindings: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class McpRequiredStartupResult:
    ready_server_ids: tuple[str, ...]


class McpRequiredStartupError(RuntimeError):
    def __init__(
        self,
        *,
        server_ids: tuple[str, ...],
        reason_code: str,
        diagnostics: tuple[McpDiagnosticFact, ...] = (),
    ) -> None:
        super().__init__(f"required MCP servers unavailable: {', '.join(server_ids)}")
        self.server_ids = server_ids
        self.reason_code = reason_code
        self.diagnostics = diagnostics


class McpDrainError(RuntimeError):
    pass


def new_mcp_slot(
    *,
    spec: McpServerRuntimeSpec,
    snapshot: McpServerSnapshot,
    manager: Any,
) -> McpManagerSlot:
    return McpManagerSlot(
        slot_id=f"mcp_slot:{uuid4().hex}",
        server_id=spec.config.server_id,
        config_epoch=snapshot.config_epoch,
        runtime_config_fingerprint=spec.runtime_config_fingerprint,
        snapshot_id=snapshot.snapshot_id,
        discovery_generation=snapshot.discovery_generation,
        manager=manager,
    )


@dataclass(frozen=True, slots=True)
class McpOriginalRequest:
    source_method: McpRequestSourceMethod
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    resource_uri: str | None = None
    prompt_name: str | None = None
    prompt_arguments: dict[str, str] | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"source_method": self.source_method.value}
        if self.tool_name is not None:
            payload["tool_name"] = self.tool_name
        if self.arguments is not None:
            payload["arguments"] = dict(self.arguments)
        if self.resource_uri is not None:
            payload["resource_uri"] = self.resource_uri
        if self.prompt_name is not None:
            payload["prompt_name"] = self.prompt_name
        if self.prompt_arguments is not None:
            payload["prompt_arguments"] = dict(self.prompt_arguments)
        return payload


@dataclass(frozen=True, slots=True)
class McpInputRequestDTO:
    key: str
    method: str
    params: dict[str, Any]

    def to_dict(self) -> dict[str, object]:
        return {"key": self.key, "method": self.method, "params": dict(self.params)}


@dataclass(frozen=True, slots=True)
class McpInputRequired:
    interaction_id: str
    server_id: str
    protocol_version: str | None
    request_state: str | None
    input_requests: tuple[McpInputRequestDTO, ...]
    original_request: McpOriginalRequest
    round_count: int = 1
    deadline_monotonic: float | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "interaction_id": self.interaction_id,
            "kind": "mcp_input_required",
            "server_id": self.server_id,
            "protocol_version": self.protocol_version,
            "request_state": self.request_state,
            "input_requests": [request.to_dict() for request in self.input_requests],
            "original_request": self.original_request.to_dict(),
            "round_count": self.round_count,
        }
        if self.deadline_monotonic is not None:
            payload["deadline_monotonic"] = self.deadline_monotonic
        return payload


@dataclass(frozen=True, slots=True)
class McpInputRequiredResolution:
    interaction_id: str
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    cancelled: bool = False
    tool_call_id: str | None = None
    input_requests: tuple[McpInputRequestDTO, ...] = ()
    round_count: int = 1
    deadline_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class McpContentArtifact:
    role: str
    media_type: str
    text: str | None = None
    data: bytes | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.text is None) == (self.data is None):
            raise ValueError("McpContentArtifact requires exactly one of text or data")


@dataclass(frozen=True, slots=True)
class McpToolResult:
    output: str
    is_error: bool = False
    structured_content: Any = None
    artifacts: tuple[McpContentArtifact, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


_SAFE_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9_-]+")
MAX_MCP_MODEL_TOOL_NAME_CHARS = 64
_MCP_TOOL_NAME_OVERHEAD = len("mcp__") + len("__") + len("__")
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_KEY_PATTERN = r"x-api-key|api[_-]?key|access[_-]?token|auth[_-]?token|bearer[_-]?token|password|secret|token"
_AUTH_HEADER_RE = re.compile(r"(?i)\b(authorization\s*:\s*)((?:Bearer|Basic)\s+)?([^\s,;}]+)")
_SECRET_QUOTED_VALUE_RE = re.compile(
    rf"(?i)([\"']?(?:{_SECRET_KEY_PATTERN})[\"']?\s*:\s*)([\"'])([^\r\n\"']*)([\"'])"
)
_SECRET_COLON_RE = re.compile(rf"(?i)\b({_SECRET_KEY_PATTERN})\s*:\s*([^\s,;}}]+)")
_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?i)\b({_SECRET_KEY_PATTERN})=([^\s&]+)"
)


def normalize_mcp_identifier(value: str) -> str:
    normalized = _SAFE_IDENTIFIER_RE.sub("_", str(value).strip()).strip("_")
    return normalized


def runtime_mcp_config_fingerprint(config: McpServerConfig) -> str:
    return _sha256_json(_runtime_config_payload(config))


def event_safe_mcp_config_fingerprint(config: McpServerConfig) -> str:
    return _sha256_json(_event_safe_config_payload(config))


def mcp_config_set_fingerprint(
    configs: tuple[McpServerConfig, ...],
    *,
    event_safe: bool,
) -> str:
    payload = {
        config.server_id: (
            _event_safe_config_payload(config)
            if event_safe
            else _runtime_config_payload(config)
        )
        for config in sorted(configs, key=lambda item: item.server_id)
    }
    return _sha256_json(payload)


def snapshot_semantic_fingerprint(
    *,
    server_id: str,
    status: McpServerStatus,
    tools: tuple[McpDiscoveredTool, ...] = (),
    resources: tuple[McpDiscoveredResource, ...] = (),
    resource_templates: tuple[McpDiscoveredResourceTemplate, ...] = (),
    prompts: tuple[McpDiscoveredPrompt, ...] = (),
    protocol_version: str | None = None,
    server_info: Mapping[str, Any] | None = None,
    instructions: str | None = None,
) -> str:
    return _sha256_json(
        {
            "server_id": server_id,
            "status": status.value,
            "tools": [
                {
                    "name": item.name,
                    "description": item.description,
                    "input_schema": item.input_schema,
                    "annotations": item.annotations.to_dict(),
                }
                for item in tools
            ],
            "resources": [
                {
                    "uri": item.uri,
                    "name": item.name,
                    "description": item.description,
                    "mime_type": item.mime_type,
                    "size": item.size,
                }
                for item in resources
            ],
            "resource_templates": [
                {
                    "uri_template": item.uri_template,
                    "name": item.name,
                    "description": item.description,
                    "mime_type": item.mime_type,
                }
                for item in resource_templates
            ],
            "prompts": [
                {
                    "name": item.name,
                    "description": item.description,
                    "arguments": list(item.arguments),
                }
                for item in prompts
            ],
            "protocol_version": protocol_version,
            "server_info": dict(server_info or {}),
            "instructions": instructions,
        }
    )


def filter_mcp_tools(
    config: McpServerConfig,
    tools: tuple[McpDiscoveredTool, ...],
) -> tuple[McpDiscoveredTool, ...]:
    if not config.enabled:
        return ()
    disabled = set(config.disabled_tools)
    enabled = set(config.enabled_tools or ())
    return tuple(
        tool
        for tool in tools
        if tool.name not in disabled and (not enabled or tool.name in enabled)
    )


def _runtime_config_payload(config: McpServerConfig) -> dict[str, object]:
    transport = config.transport
    if isinstance(transport, McpStdioConfig):
        transport_payload: dict[str, object] = {
            "kind": "stdio",
            "command": transport.command,
            "args": list(transport.args),
            "env": dict(transport.env),
            "cwd": str(transport.cwd) if transport.cwd is not None else None,
        }
    else:
        transport_payload = {
            "kind": "streamable_http",
            "url": transport.url,
            "bearer_token_env_var": transport.bearer_token_env_var,
            "bearer_token_value_fingerprint": _runtime_secret_fingerprint(
                os.getenv(transport.bearer_token_env_var)
                if transport.bearer_token_env_var
                else None
            ),
            "headers": dict(transport.headers),
            "env_headers": dict(transport.env_headers),
            "env_header_value_fingerprints": {
                header: _runtime_secret_fingerprint(os.getenv(env_name))
                for header, env_name in sorted(transport.env_headers.items())
            },
            "follow_redirects": transport.follow_redirects,
        }
    return {**_common_config_payload(config), "transport": transport_payload}


def _event_safe_config_payload(config: McpServerConfig) -> dict[str, object]:
    transport = config.transport
    if isinstance(transport, McpStdioConfig):
        transport_payload: dict[str, object] = {
            "kind": "stdio",
            "command": transport.command,
            "args": list(transport.args),
            "env_keys": sorted(transport.env),
            "cwd": str(transport.cwd) if transport.cwd is not None else None,
        }
    else:
        split = urlsplit(transport.url)
        host = (split.hostname or "").lower()
        port = split.port
        if port is not None and not (
            (split.scheme == "https" and port == 443)
            or (split.scheme == "http" and port == 80)
        ):
            host = f"{host}:{port}"
        transport_payload = {
            "kind": "streamable_http",
            "endpoint": urlunsplit((split.scheme.lower(), host, split.path or "/", "", "")),
            "bearer_token_env_var": transport.bearer_token_env_var,
            "bearer_token_present": bool(
                transport.bearer_token_env_var
                and os.getenv(transport.bearer_token_env_var)
            ),
            "header_keys": sorted(str(key).lower() for key in transport.headers),
            "env_headers": dict(sorted(transport.env_headers.items())),
            "env_header_presence": {
                header: bool(os.getenv(env_name))
                for header, env_name in sorted(transport.env_headers.items())
            },
            "follow_redirects": transport.follow_redirects,
        }
    return {**_common_config_payload(config), "transport": transport_payload}


def _common_config_payload(config: McpServerConfig) -> dict[str, object]:
    return {
        "server_id": config.server_id,
        "enabled": config.enabled,
        "required": config.required,
        "connect_timeout_ms": config.connect_timeout_ms,
        "discovery_timeout_ms": config.discovery_timeout_ms,
        "startup_deadline_ms": config.startup_deadline_ms,
        "refresh_ttl_ms": config.refresh_ttl_ms,
        "tool_timeout_ms": config.tool_timeout_ms,
        "supports_parallel_tool_calls": config.supports_parallel_tool_calls,
        "enabled_tools": config.enabled_tools,
        "disabled_tools": config.disabled_tools,
        "default_approval_mode": config.default_approval_mode,
    }


def _runtime_secret_fingerprint(value: str | None) -> str | None:
    if value is None:
        return None
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(payload: object) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def mangle_mcp_tool_name(server_id: str, tool_name: str, *, max_length: int = MAX_MCP_MODEL_TOOL_NAME_CHARS) -> str:
    safe_server = normalize_mcp_identifier(server_id) or "server"
    safe_tool = normalize_mcp_identifier(tool_name) or "tool"
    full = f"mcp__{safe_server}__{safe_tool}"
    if len(full) <= max_length:
        return full
    digest = hashlib.sha256(f"{server_id}\0{tool_name}".encode("utf-8")).hexdigest()[:10]
    budget = max_length - _MCP_TOOL_NAME_OVERHEAD - len(digest)
    if budget < 2:
        return f"mcp__{digest}"[:max_length]
    server_budget = max(1, budget // 2)
    tool_budget = max(1, budget - server_budget)
    if server_budget + tool_budget > budget:
        tool_budget = max(1, budget - server_budget)
    return f"mcp__{safe_server[:server_budget]}__{safe_tool[:tool_budget]}__{digest}"


def redact_mcp_error_message(message: object) -> str:
    """Best-effort redaction for runtime MCP errors before durable events."""

    text = str(message)
    text = _HTTP_URL_RE.sub(lambda match: _redact_url_text(match.group(0)), text)
    text = _AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)}[redacted]", text)
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = _SECRET_QUOTED_VALUE_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[redacted]{match.group(4)}",
        text,
    )
    text = _SECRET_COLON_RE.sub(lambda match: f"{match.group(1)}: [redacted]", text)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    return text


def _redact_url_text(url: str) -> str:
    trailing = ""
    while url and url[-1] in ".,;:)":
        trailing = url[-1] + trailing
        url = url[:-1]
    try:
        split = urlsplit(url)
    except ValueError:
        return url + trailing
    netloc = split.netloc
    if "@" in netloc:
        netloc = f"[redacted]@{netloc.rsplit('@', 1)[1]}"
    query = "[redacted]" if split.query else ""
    fragment = "[redacted]" if split.fragment else ""
    return urlunsplit(
        SplitResult(
            scheme=split.scheme,
            netloc=netloc,
            path=split.path,
            query=query,
            fragment=fragment,
        )
    ) + trailing


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
