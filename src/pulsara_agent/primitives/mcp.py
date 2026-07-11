"""Durable, event-safe MCP facts.

This module is intentionally below the runtime and SDK layers.  It must never
hold live managers, transport configuration, credentials, or HostSession
references.
"""

from __future__ import annotations

import math
import json
from datetime import datetime, timezone
from typing import Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


McpServerStatusValue = Literal[
    "disabled",
    "starting",
    "ready",
    "degraded",
    "failed",
    "needs_auth",
    "closing",
    "closed",
]
McpReconcileTriggerValue = Literal[
    "initial",
    "config_change",
    "ttl_refresh",
    "retry",
    "manual_refresh",
]

MAX_MCP_DIAGNOSTIC_CODE_CHARS = 128
MAX_MCP_DIAGNOSTIC_MESSAGE_CHARS = 1_024
MAX_MCP_DIAGNOSTIC_METADATA_CHARS = 4_096
MAX_MCP_DIAGNOSTICS_PER_FACT = 16


class FrozenMcpJsonDict(dict[str, object]):
    """JSON-serializable recursively immutable mapping used by MCP facts.

    ``MappingProxyType`` is a good process-local guard but is not accepted by
    Pydantic's JSON serializer.  This small dict subclass preserves normal JSON
    serialization while rejecting every mutation entry point.
    """

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("MCP JSON fact is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __copy__(self) -> "FrozenMcpJsonDict":
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> "FrozenMcpJsonDict":
        return self


def freeze_mcp_json_value(value: Any) -> Any:
    """Recursively freeze a JSON-compatible value without losing serialization."""

    if isinstance(value, FrozenMcpJsonDict):
        return value
    if isinstance(value, Mapping):
        frozen = FrozenMcpJsonDict()
        for key, item in value.items():
            dict.__setitem__(frozen, str(key), freeze_mcp_json_value(item))
        return frozen
    if isinstance(value, (list, tuple)):
        return tuple(freeze_mcp_json_value(item) for item in value)
    return value


def thaw_mcp_json_value(value: Any) -> Any:
    """Return ordinary JSON containers at Inspector/adapter boundaries."""

    if isinstance(value, Mapping):
        return {str(key): thaw_mcp_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_mcp_json_value(item) for item in value]
    return value


class McpFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class McpDiagnosticFact(McpFact):
    severity: Literal["info", "warning", "error"] = "warning"
    code: str
    message: str
    metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("code", "message")
    @classmethod
    def _non_empty(cls, value: str, info: ValidationInfo) -> str:
        value = value.strip()
        if not value:
            raise ValueError("MCP diagnostic fields must be non-empty")
        limit = (
            MAX_MCP_DIAGNOSTIC_CODE_CHARS
            if info.field_name == "code"
            else MAX_MCP_DIAGNOSTIC_MESSAGE_CHARS
        )
        if len(value) > limit:
            raise ValueError("MCP diagnostic field exceeds bounded character cap")
        return value

    @field_validator("metadata", mode="after")
    @classmethod
    def _freeze_metadata(cls, value: dict[str, object]) -> dict[str, object]:
        _require_mcp_json_value(value)
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
        if len(serialized) > MAX_MCP_DIAGNOSTIC_METADATA_CHARS:
            raise ValueError("MCP diagnostic metadata exceeds bounded character cap")
        return freeze_mcp_json_value(value)


class McpServerInfoFact(McpFact):
    name: str | None = None
    title: str | None = None
    version: str | None = None


class McpServerLifecycleTimingFact(McpFact):
    queued_at_utc: str
    connect_started_at_utc: str | None = None
    connect_ended_at_utc: str | None = None
    discovery_started_at_utc: str | None = None
    discovery_ended_at_utc: str | None = None
    completed_at_utc: str | None = None
    connect_duration_seconds: float | None = None
    discovery_duration_seconds: float | None = None
    total_duration_seconds: float | None = None

    @field_validator(
        "queued_at_utc",
        "connect_started_at_utc",
        "connect_ended_at_utc",
        "discovery_started_at_utc",
        "discovery_ended_at_utc",
        "completed_at_utc",
    )
    @classmethod
    def _utc_timestamp(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("MCP lifecycle timestamps must be timezone-aware")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @field_validator(
        "connect_duration_seconds",
        "discovery_duration_seconds",
        "total_duration_seconds",
    )
    @classmethod
    def _finite_duration(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("MCP lifecycle durations must be finite and non-negative")
        return value


class McpDiscoveredToolFact(McpFact):
    server_id: str
    name: str
    description: str
    input_schema: dict[str, object]
    annotations: dict[str, object] = Field(default_factory=dict)

    @field_validator("input_schema", "annotations", mode="after")
    @classmethod
    def _freeze_mappings(cls, value: dict[str, object]) -> dict[str, object]:
        return freeze_mcp_json_value(value)


class McpDiscoveredResourceFact(McpFact):
    server_id: str
    uri: str
    name: str
    description: str = ""
    mime_type: str | None = None
    size: int | None = None


class McpDiscoveredResourceTemplateFact(McpFact):
    server_id: str
    uri_template: str
    name: str
    description: str = ""
    mime_type: str | None = None


class McpDiscoveredPromptFact(McpFact):
    server_id: str
    name: str
    description: str = ""
    arguments: tuple[dict[str, object], ...] = ()

    @field_validator("arguments", mode="after")
    @classmethod
    def _freeze_arguments(
        cls,
        value: tuple[dict[str, object], ...],
    ) -> tuple[dict[str, object], ...]:
        return tuple(freeze_mcp_json_value(item) for item in value)


class McpServerSnapshotFact(McpFact):
    snapshot_id: str
    server_id: str
    config_epoch: int
    event_safe_config_fingerprint: str
    snapshot_semantic_fingerprint: str
    reconcile_attempt_id: str
    discovery_generation: int
    status: McpServerStatusValue
    required: bool
    tools: tuple[McpDiscoveredToolFact, ...] = ()
    resources: tuple[McpDiscoveredResourceFact, ...] = ()
    resource_templates: tuple[McpDiscoveredResourceTemplateFact, ...] = ()
    prompts: tuple[McpDiscoveredPromptFact, ...] = ()
    protocol_version: str | None = None
    server_info: McpServerInfoFact | None = None
    instructions: str | None = None
    timing: McpServerLifecycleTimingFact
    diagnostics: tuple[McpDiagnosticFact, ...] = ()

    @model_validator(mode="after")
    def _status_contract(self) -> "McpServerSnapshotFact":
        if self.config_epoch < 0 or self.discovery_generation < 0:
            raise ValueError("MCP snapshot generations must be non-negative")
        if self.status != "ready" and (
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
            raise ValueError("MCP snapshot fact discovered item server_id mismatch")
        if self.status not in {"starting", "closing"}:
            if self.timing.completed_at_utc is None or self.timing.total_duration_seconds is None:
                raise ValueError("terminal MCP snapshot requires completed timing")
        if self.status == "ready" and (
            self.timing.connect_ended_at_utc is None
            or self.timing.discovery_ended_at_utc is None
        ):
            raise ValueError("ready MCP snapshot requires completed connect/discovery timing")
        return self


class McpConfigSetFact(McpFact):
    config_epoch: int
    event_safe_config_set_fingerprint: str
    event_safe_server_config_fingerprints: dict[str, str]
    server_ids: tuple[str, ...]


class McpReconcileAttemptSummaryFact(McpFact):
    server_id: str
    reconcile_attempt_id: str
    reconcile_trigger: McpReconcileTriggerValue
    attempt_status: Literal[
        "scheduled", "running", "ready", "degraded", "failed", "needs_auth", "disabled"
    ]
    retry_attempt: int = 0
    request_count: int = 0
    page_count: int = 0
    cache_outcome: Literal[
        "not_applicable",
        "miss",
        "ttl_fresh_reuse",
        "config_fingerprint_reuse",
        "sdk_response_cache_hit",
    ] = "not_applicable"
    stale_candidates_discarded_since_previous_install: int = 0

    @model_validator(mode="after")
    def _counts(self) -> "McpReconcileAttemptSummaryFact":
        values = (
            self.retry_attempt,
            self.request_count,
            self.page_count,
            self.stale_candidates_discarded_since_previous_install,
        )
        if any(value < 0 for value in values):
            raise ValueError("MCP attempt counts must be non-negative")
        if self.page_count > self.request_count:
            raise ValueError("MCP attempt page_count cannot exceed request_count")
        if self.cache_outcome in {
            "ttl_fresh_reuse",
            "config_fingerprint_reuse",
            "sdk_response_cache_hit",
        } and (self.request_count or self.page_count):
            raise ValueError("cache reuse cannot report network request/page counts")
        return self


class McpInstalledServerSnapshotFact(McpFact):
    server_id: str
    status: McpServerStatusValue
    required: bool
    changed_in_this_installation: bool
    attempt: McpReconcileAttemptSummaryFact
    snapshot_id: str
    discovery_generation: int
    event_safe_config_fingerprint: str
    snapshot_semantic_fingerprint: str
    protocol_version: str | None = None
    tool_count: int = 0
    resource_count: int = 0
    resource_template_count: int = 0
    prompt_count: int = 0
    instructions_chars: int = 0
    lifecycle_timing: McpServerLifecycleTimingFact
    diagnostics: tuple[McpDiagnosticFact, ...] = ()
    catalog_artifact_id: None

    @model_validator(mode="after")
    def _attribution(self) -> "McpInstalledServerSnapshotFact":
        if self.attempt.server_id != self.server_id:
            raise ValueError("MCP installed snapshot attempt server_id mismatch")
        counts = (
            self.discovery_generation,
            self.tool_count,
            self.resource_count,
            self.resource_template_count,
            self.prompt_count,
            self.instructions_chars,
        )
        if any(value < 0 for value in counts):
            raise ValueError("MCP installed snapshot counts must be non-negative")
        if len(self.diagnostics) > MAX_MCP_DIAGNOSTICS_PER_FACT:
            raise ValueError("MCP installed snapshot diagnostics exceed bounded cap")
        if self.status not in {"starting", "closing"} and (
            self.lifecycle_timing.completed_at_utc is None
            or self.lifecycle_timing.total_duration_seconds is None
        ):
            raise ValueError("terminal MCP installed snapshot requires completed timing")
        if self.status == "ready" and (
            self.lifecycle_timing.connect_ended_at_utc is None
            or self.lifecycle_timing.discovery_ended_at_utc is None
        ):
            raise ValueError(
                "ready MCP installed snapshot requires completed connect/discovery timing"
            )
        return self


def _require_mcp_json_value(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("MCP JSON facts require finite numbers")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("MCP JSON fact mapping keys must be strings")
            _require_mcp_json_value(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _require_mcp_json_value(item)
        return
    raise ValueError(f"MCP JSON fact contains unsupported type: {type(value).__name__}")


__all__ = [
    "FrozenMcpJsonDict",
    "McpConfigSetFact",
    "McpDiagnosticFact",
    "McpDiscoveredPromptFact",
    "McpDiscoveredResourceFact",
    "McpDiscoveredResourceTemplateFact",
    "McpDiscoveredToolFact",
    "McpInstalledServerSnapshotFact",
    "McpReconcileAttemptSummaryFact",
    "McpReconcileTriggerValue",
    "McpServerInfoFact",
    "McpServerLifecycleTimingFact",
    "McpServerSnapshotFact",
    "McpServerStatusValue",
    "MAX_MCP_DIAGNOSTIC_CODE_CHARS",
    "MAX_MCP_DIAGNOSTIC_MESSAGE_CHARS",
    "MAX_MCP_DIAGNOSTICS_PER_FACT",
    "freeze_mcp_json_value",
    "thaw_mcp_json_value",
]
