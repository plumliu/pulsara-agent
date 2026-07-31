"""Capability descriptors for the unified capability surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pulsara_agent.primitives.model_call import sha256_fingerprint
from pulsara_agent.primitives.long_horizon import LongHorizonToolPolicyFact
from pulsara_agent.primitives.tool_result import CapabilityResultRenderContractFact
from pulsara_agent.ports.artifact import ToolArtifactMode
from pulsara_agent.ports.tool_execution import (
    FrozenToolJsonDict,
    freeze_tool_json_object,
)


class CapabilityProviderKind(StrEnum):
    BUILTIN = "builtin"
    WORKFLOW = "workflow"
    MEMORY = "memory"
    SKILL = "skill"
    MCP = "mcp"


class CapabilityAvailability(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class CapabilityAdvertisePolicy(StrEnum):
    DIRECT = "direct"
    DEFERRED = "deferred"
    HIDDEN = "hidden"


@dataclass(frozen=True, slots=True)
class CapabilityProvenance:
    provider_kind: CapabilityProviderKind
    provider_id: str
    source: str | None = None
    version: str | None = None
    owner: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    id: str
    name: str
    description: str
    input_schema: FrozenToolJsonDict | None
    namespace: str | None
    provider_kind: CapabilityProviderKind
    provider_id: str
    is_model_callable: bool
    is_read_only: bool
    is_concurrency_safe: bool
    result_render_contract: CapabilityResultRenderContractFact
    long_horizon_policy: LongHorizonToolPolicyFact
    is_destructive: bool = False
    is_open_world: bool = False
    requires_user_interaction: bool = False
    permission_category: str = "general"
    approval_policy_hint: str | None = None
    advertise_policy: CapabilityAdvertisePolicy = CapabilityAdvertisePolicy.DIRECT
    artifact_mode: ToolArtifactMode = ToolArtifactMode.DEFAULT
    max_inline_chars: int | None = None
    timeout_ms: int | None = None
    availability: CapabilityAvailability = CapabilityAvailability.AVAILABLE
    health_message: str | None = None
    provenance: CapabilityProvenance | None = None
    metadata: FrozenToolJsonDict = field(default_factory=FrozenToolJsonDict)

    def __post_init__(self) -> None:
        if self.input_schema is not None:
            object.__setattr__(
                self,
                "input_schema",
                freeze_tool_json_object(self.input_schema),
            )
        object.__setattr__(
            self,
            "metadata",
            freeze_tool_json_object(self.metadata),
        )

    def to_diagnostic_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "provider_kind": self.provider_kind.value,
            "provider_id": self.provider_id,
            "is_model_callable": self.is_model_callable,
            "is_read_only": self.is_read_only,
            "is_concurrency_safe": self.is_concurrency_safe,
            "is_destructive": self.is_destructive,
            "is_open_world": self.is_open_world,
            "permission_category": self.permission_category,
            "result_render_contract": self.result_render_contract.model_dump(
                mode="json"
            ),
            "long_horizon_policy": self.long_horizon_policy.model_dump(mode="json"),
            "advertise_policy": self.advertise_policy.value,
            "artifact_mode": self.artifact_mode.value,
            "availability": self.availability.value,
            "health_message": self.health_message,
        }

    def to_event_payload(self) -> dict[str, object]:
        provenance = self.provenance
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "namespace": self.namespace,
            "provider_kind": self.provider_kind.value,
            "provider_id": self.provider_id,
            "is_model_callable": self.is_model_callable,
            "is_read_only": self.is_read_only,
            "is_concurrency_safe": self.is_concurrency_safe,
            "is_destructive": self.is_destructive,
            "is_open_world": self.is_open_world,
            "requires_user_interaction": self.requires_user_interaction,
            "permission_category": self.permission_category,
            "result_render_contract": self.result_render_contract.model_dump(
                mode="json"
            ),
            "long_horizon_policy": self.long_horizon_policy.model_dump(mode="json"),
            "approval_policy_hint": self.approval_policy_hint,
            "advertise_policy": self.advertise_policy.value,
            "artifact_mode": self.artifact_mode.value,
            "max_inline_chars": self.max_inline_chars,
            "timeout_ms": self.timeout_ms,
            "availability": self.availability.value,
            "health_message": self.health_message,
            "provenance": (
                {
                    "provider_kind": provenance.provider_kind.value,
                    "provider_id": provenance.provider_id,
                    "source": provenance.source,
                    "version": provenance.version,
                    "owner": provenance.owner,
                }
                if provenance is not None
                else None
            ),
            "metadata": self.metadata,
        }

    def fingerprint(self) -> str:
        return sha256_fingerprint("capability-descriptor:v1", self.to_event_payload())
