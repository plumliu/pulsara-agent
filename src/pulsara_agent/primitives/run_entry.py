"""Event-safe run-entry identity and current-user contracts."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pulsara_agent.primitives.capability_owner import (
    CapabilityExposureOwnerFact,
    CapabilityExposureOwnerKind,
)
from pulsara_agent.primitives.run_boundary_kind import HostRunBoundaryKind
from pulsara_agent.primitives.subagent import ChildResultRenderPolicyFact

if TYPE_CHECKING:
    from pulsara_agent.primitives.capability import CapabilityResolveBasisFact
    from pulsara_agent.primitives.host_ingress import HostRunIngressFact


class RunEntryKind(StrEnum):
    HOST = "host"
    SUBAGENT_CHILD = "subagent_child"


class DurableRunExistence(StrEnum):
    NONE = "none"
    FULL = "full"
    UNKNOWN = "unknown"
    PARTIAL_UNTRUSTED = "partial_untrusted"


def canonical_utc_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class HostRunBoundaryIdentityFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    boundary_id: str = Field(min_length=1)
    kind: HostRunBoundaryKind
    runtime_session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    reply_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    observed_at_utc: str

    @field_validator("observed_at_utc")
    @classmethod
    def _utc(cls, value: str) -> str:
        return canonical_utc_timestamp(value)


class CurrentUserMessageFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: str = Field(min_length=1)
    source_kind: Literal[
        "host_user_input",
        "host_runtime_request",
        "subagent_task",
        "subagent_primitive_objective",
    ]
    text: str
    observed_at_utc: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_artifact_id: str | None

    @field_validator("observed_at_utc")
    @classmethod
    def _utc(cls, value: str) -> str:
        return canonical_utc_timestamp(value)

    @model_validator(mode="after")
    def _validate_message(self) -> "CurrentUserMessageFact":
        if self.content_sha256 != text_sha256(self.text):
            raise ValueError("current user content_sha256 mismatch")
        if self.source_kind in {"host_user_input", "host_runtime_request"}:
            if self.source_artifact_id is not None:
                raise ValueError("host current user cannot reference an artifact")
        elif not self.source_artifact_id:
            raise ValueError("subagent current user requires a source artifact")
        return self


class SubagentRunEntryFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subagent_run_id: str = Field(min_length=1)
    subagent_task_id: str | None
    parent_runtime_session_id: str = Field(min_length=1)
    parent_run_id: str = Field(min_length=1)
    spawn_edge_id: str = Field(min_length=1)
    capability_profile_fingerprint: str = Field(min_length=1)
    task_artifact_id: str = Field(min_length=1)
    task_observed_at_utc: str
    child_result_render_policy: ChildResultRenderPolicyFact
    permission_snapshot_id: str = Field(min_length=1)
    model_target_fingerprint: str = Field(min_length=1)
    mcp_installation_id: str = Field(min_length=1)
    mcp_installation_owner_runtime_session_id: str = Field(min_length=1)
    capability_basis: "CapabilityResolveBasisFact"

    @field_validator("task_observed_at_utc")
    @classmethod
    def _utc(cls, value: str) -> str:
        return canonical_utc_timestamp(value)

    @model_validator(mode="after")
    def _validate_owner(self) -> "SubagentRunEntryFact":
        if (
            self.mcp_installation_owner_runtime_session_id
            != self.parent_runtime_session_id
        ):
            raise ValueError("child MCP installation must be parent-owned")
        basis = self.capability_basis
        owner = basis.owner
        if basis.basis_kind != "initial":
            raise ValueError("child capability basis must be initial")
        if (
            owner.owner_kind is not CapabilityExposureOwnerKind.SUBAGENT_RUN_START
            or owner.host_boundary_kind is not None
        ):
            raise ValueError("child capability basis requires a subagent owner")
        if basis.permission_snapshot_id != self.permission_snapshot_id:
            raise ValueError("child capability basis permission mismatch")
        if basis.mcp_installation_id != self.mcp_installation_id:
            raise ValueError("child capability basis MCP installation mismatch")
        return self


def validate_host_current_user_attribution(
    *,
    boundary: HostRunBoundaryIdentityFact,
    current_user: CurrentUserMessageFact,
    ingress: "HostRunIngressFact",
) -> None:
    from pulsara_agent.primitives.host_ingress import (
        HumanRunIngressFact,
        RuntimeRequestRunIngressFact,
    )
    from pulsara_agent.primitives.runtime_observation import (
        RuntimeTaskRequestPayloadFact,
    )

    if isinstance(ingress, HumanRunIngressFact):
        if boundary.kind is not HostRunBoundaryKind.PRE_RUN:
            raise ValueError("human Host ingress requires a pre-run boundary")
        if current_user.source_kind != "host_user_input":
            raise ValueError("human Host ingress requires host_user_input")
        expected_text = ingress.human_message.text
    elif isinstance(ingress, RuntimeRequestRunIngressFact):
        if boundary.kind is not HostRunBoundaryKind.PRE_RUNTIME_REQUEST:
            raise ValueError("runtime Host ingress requires a runtime-request boundary")
        if current_user.source_kind != "host_runtime_request":
            raise ValueError("runtime Host ingress requires host_runtime_request")
        payload = ingress.runtime_request.payload
        if not isinstance(payload, RuntimeTaskRequestPayloadFact):
            raise ValueError("Host runtime ingress requires a task payload")
        expected_text = payload.task_text
    else:  # pragma: no cover - the discriminated union is closed.
        raise TypeError(type(ingress))
    if current_user.text != expected_text:
        raise ValueError("Host current input text differs from typed ingress")
    if (
        current_user.observed_at_utc != boundary.observed_at_utc
        or ingress.attribution.observed_at_utc != boundary.observed_at_utc
    ):
        raise ValueError("host current user observation must match boundary ingress")


def validate_subagent_current_user_attribution(
    *,
    entry: SubagentRunEntryFact,
    current_user: CurrentUserMessageFact,
) -> None:
    expected_kind = (
        "subagent_task"
        if entry.subagent_task_id is not None
        else "subagent_primitive_objective"
    )
    if current_user.source_kind != expected_kind:
        raise ValueError("subagent current user source kind does not match entry mode")
    if current_user.source_artifact_id != entry.task_artifact_id:
        raise ValueError("subagent current user artifact does not match entry")
    if current_user.observed_at_utc != entry.task_observed_at_utc:
        raise ValueError("subagent current user observation does not match entry")


__all__ = [
    "CapabilityExposureOwnerFact",
    "CapabilityExposureOwnerKind",
    "CurrentUserMessageFact",
    "DurableRunExistence",
    "HostRunBoundaryIdentityFact",
    "HostRunBoundaryKind",
    "RunEntryKind",
    "SubagentRunEntryFact",
    "canonical_utc_timestamp",
    "text_sha256",
    "validate_host_current_user_attribution",
    "validate_subagent_current_user_attribution",
]


# Resolve the one deliberate forward reference after capability.py has defined
# CapabilityResolveBasisFact.  capability.py invokes this helper at module end.
def _rebuild_subagent_run_entry_fact(
    capability_resolve_basis_type: type[BaseModel],
) -> None:
    SubagentRunEntryFact.model_rebuild(
        _types_namespace={
            "CapabilityResolveBasisFact": capability_resolve_basis_type,
        }
    )
