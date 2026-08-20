"""Pure, provider-neutral contracts for Round 9 capability semantics.

This module intentionally owns values only.  It does not discover skills,
connect MCP servers, resolve model transports, read a repository, or retain an
executor.  Physical owners issue their own opaque snapshot carriers and the
single registry factory admits only the semantic values extracted from those
carriers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from pulsara_agent.model_input.contracts import (
    FrozenModelToolSurface,
    FrozenToolSpec,
    ModelInputScopeKind,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
)

MAXIMUM_CAPABILITY_MCP_SOURCES = 64
MAXIMUM_CAPABILITY_SOURCE_REGISTRATIONS = 66
MAXIMUM_CAPABILITY_PLANNER_FRAMING_BYTES = 4 * 1024 * 1024
MAXIMUM_NATIVE_TOOL_COUNT = 64
MAXIMUM_NATIVE_TOOL_BYTES = 1024 * 1024


class CapabilityContractError(RuntimeError):
    """A closed capability value failed an exact semantic join."""


class CapabilityKind(StrEnum):
    TOOL = "TOOL"
    SKILL = "SKILL"


class CapabilitySourceKind(StrEnum):
    BUILTIN_REGISTRY = "BUILTIN_REGISTRY"
    MCP_SERVER = "MCP_SERVER"
    LOCAL_SKILL_CATALOG = "LOCAL_SKILL_CATALOG"


class LocalSkillRootKind(StrEnum):
    WORKSPACE_PULSARA = "WORKSPACE_PULSARA"
    WORKSPACE_AGENTS = "WORKSPACE_AGENTS"
    USER_PULSARA = "USER_PULSARA"
    USER_AGENTS = "USER_AGENTS"


@dataclass(frozen=True, slots=True)
class CapabilitySourceRef:
    kind: CapabilitySourceKind
    stable_source_id: str
    source_identity_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CapabilitySourceKind):
            raise TypeError("capability source kind is not closed")
        if not self.stable_source_id:
            raise ValueError("capability source id is empty")
        expected = context_fingerprint(
            "capability-source-identity:v1",
            (self.kind.value, self.stable_source_id),
        )
        if self.source_identity_fingerprint != expected:
            raise ValueError("capability source identity fingerprint mismatch")


def capability_source_ref(
    kind: CapabilitySourceKind, stable_source_id: str
) -> CapabilitySourceRef:
    return CapabilitySourceRef(
        kind=kind,
        stable_source_id=stable_source_id,
        source_identity_fingerprint=context_fingerprint(
            "capability-source-identity:v1", (kind.value, stable_source_id)
        ),
    )


@dataclass(frozen=True, slots=True)
class CapabilityIdentity:
    kind: CapabilityKind
    source: CapabilitySourceRef
    stable_name: str
    identity_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CapabilityKind):
            raise TypeError("capability kind is not closed")
        if not isinstance(self.source, CapabilitySourceRef):
            raise TypeError("capability source is not frozen")
        if not self.stable_name:
            raise ValueError("capability stable name is empty")
        expected = context_fingerprint(
            "capability-identity:v1",
            {
                "kind": self.kind.value,
                "source_kind": self.source.kind.value,
                "stable_source_id": self.source.stable_source_id,
                "stable_name": self.stable_name,
            },
        )
        if self.identity_fingerprint != expected:
            raise ValueError("capability identity fingerprint mismatch")


def capability_identity(
    *, kind: CapabilityKind, source: CapabilitySourceRef, stable_name: str
) -> CapabilityIdentity:
    return CapabilityIdentity(
        kind=kind,
        source=source,
        stable_name=stable_name,
        identity_fingerprint=context_fingerprint(
            "capability-identity:v1",
            {
                "kind": kind.value,
                "source_kind": source.kind.value,
                "stable_source_id": source.stable_source_id,
                "stable_name": stable_name,
            },
        ),
    )


class CapabilitySourceRefreshMode(StrEnum):
    IMMUTABLE = "IMMUTABLE"
    SAFE_POINT_REFRESHABLE = "SAFE_POINT_REFRESHABLE"


@dataclass(frozen=True, slots=True)
class FrozenCapabilitySourceRegistration:
    source: CapabilitySourceRef
    refresh_mode: CapabilitySourceRefreshMode
    source_contract_fingerprint: str
    registration_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, CapabilitySourceRef):
            raise TypeError("registration source is not frozen")
        if not isinstance(self.refresh_mode, CapabilitySourceRefreshMode):
            raise TypeError("source refresh mode is not closed")
        if not self.source_contract_fingerprint:
            raise ValueError("source contract fingerprint is empty")
        expected = context_fingerprint(
            "capability-source-registration:v1",
            {
                "source_identity_fingerprint": (
                    self.source.source_identity_fingerprint
                ),
                "refresh_mode": self.refresh_mode.value,
                "source_contract_fingerprint": self.source_contract_fingerprint,
            },
        )
        if self.registration_fingerprint != expected:
            raise ValueError("source registration fingerprint mismatch")
        expected_mode = {
            CapabilitySourceKind.BUILTIN_REGISTRY: (
                CapabilitySourceRefreshMode.IMMUTABLE
            ),
            CapabilitySourceKind.MCP_SERVER: (
                CapabilitySourceRefreshMode.SAFE_POINT_REFRESHABLE
            ),
            CapabilitySourceKind.LOCAL_SKILL_CATALOG: (
                CapabilitySourceRefreshMode.SAFE_POINT_REFRESHABLE
            ),
        }[self.source.kind]
        if self.refresh_mode is not expected_mode:
            raise ValueError("source registration refresh mode conflicts")


def capability_source_registration(
    *,
    source: CapabilitySourceRef,
    refresh_mode: CapabilitySourceRefreshMode,
    source_contract_fingerprint: str,
) -> FrozenCapabilitySourceRegistration:
    payload = {
        "source_identity_fingerprint": source.source_identity_fingerprint,
        "refresh_mode": refresh_mode.value,
        "source_contract_fingerprint": source_contract_fingerprint,
    }
    return FrozenCapabilitySourceRegistration(
        source=source,
        refresh_mode=refresh_mode,
        source_contract_fingerprint=source_contract_fingerprint,
        registration_fingerprint=context_fingerprint(
            "capability-source-registration:v1", payload
        ),
    )


def _validate_scope(
    scope: ModelInputScopeKind, subagent_task_id: str | None
) -> None:
    if not isinstance(scope, ModelInputScopeKind):
        raise TypeError("capability scope kind is not closed")
    if (scope is ModelInputScopeKind.ROOT) != (subagent_task_id is None):
        raise ValueError("capability scope identity is invalid")


@dataclass(frozen=True, slots=True)
class FrozenCapabilitySourceRegistrationSet:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    builtin_registration: FrozenCapabilitySourceRegistration
    mcp_registrations: tuple[FrozenCapabilitySourceRegistration, ...]
    local_skill_catalog_registration: FrozenCapabilitySourceRegistration
    registration_set_fingerprint: str

    def __post_init__(self) -> None:
        _validate_scope(
            self.conversation_scope_kind, self.scope_subagent_task_id
        )
        if (
            self.builtin_registration.source.kind
            is not CapabilitySourceKind.BUILTIN_REGISTRY
            or self.local_skill_catalog_registration.source.kind
            is not CapabilitySourceKind.LOCAL_SKILL_CATALOG
        ):
            raise ValueError("capability registration singleton kinds conflict")
        if len(self.mcp_registrations) > MAXIMUM_CAPABILITY_MCP_SOURCES:
            raise ValueError("MCP source registration bound exceeded")
        mcp_ids = tuple(
            item.source.stable_source_id for item in self.mcp_registrations
        )
        if mcp_ids != tuple(sorted(mcp_ids)) or len(mcp_ids) != len(set(mcp_ids)):
            raise ValueError("MCP source registrations are not sorted and unique")
        if any(
            item.source.kind is not CapabilitySourceKind.MCP_SERVER
            for item in self.mcp_registrations
        ):
            raise ValueError("non-MCP source entered MCP registrations")
        expected = capability_registration_set_fingerprint(
            conversation_scope_kind=self.conversation_scope_kind,
            scope_subagent_task_id=self.scope_subagent_task_id,
            builtin_registration=self.builtin_registration,
            mcp_registrations=self.mcp_registrations,
            local_skill_catalog_registration=(
                self.local_skill_catalog_registration
            ),
        )
        if self.registration_set_fingerprint != expected:
            raise ValueError("capability registration set fingerprint mismatch")

    @property
    def registrations(self) -> tuple[FrozenCapabilitySourceRegistration, ...]:
        return (
            self.builtin_registration,
            *self.mcp_registrations,
            self.local_skill_catalog_registration,
        )


def capability_registration_set_fingerprint(
    *,
    conversation_scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    builtin_registration: FrozenCapabilitySourceRegistration,
    mcp_registrations: tuple[FrozenCapabilitySourceRegistration, ...],
    local_skill_catalog_registration: FrozenCapabilitySourceRegistration,
) -> str:
    return context_fingerprint(
        "capability-source-registration-set:v1",
        {
            "scope": conversation_scope_kind.value,
            "scope_subagent_task_id": scope_subagent_task_id,
            "builtin": builtin_registration.registration_fingerprint,
            "mcp": tuple(
                item.registration_fingerprint for item in mcp_registrations
            ),
            "skills": local_skill_catalog_registration.registration_fingerprint,
        },
    )


class ToolCapabilityOrigin(StrEnum):
    BUILTIN = "BUILTIN"
    MCP = "MCP"


@dataclass(frozen=True, slots=True)
class FrozenToolCapabilityFact:
    identity: CapabilityIdentity
    origin: ToolCapabilityOrigin
    canonical_tool_spec: FrozenToolSpec = field(repr=False)
    semantic_fingerprint: str

    def __post_init__(self) -> None:
        if self.identity.kind is not CapabilityKind.TOOL:
            raise ValueError("tool fact identity is not TOOL")
        if not isinstance(self.origin, ToolCapabilityOrigin):
            raise TypeError("tool capability origin is not closed")
        expected_source = (
            CapabilitySourceKind.BUILTIN_REGISTRY
            if self.origin is ToolCapabilityOrigin.BUILTIN
            else CapabilitySourceKind.MCP_SERVER
        )
        if self.identity.source.kind is not expected_source:
            raise ValueError("tool fact origin/source matrix conflicts")
        if not isinstance(self.canonical_tool_spec, FrozenToolSpec):
            raise TypeError("canonical tool specification is not frozen")
        expected = tool_capability_semantic_fingerprint(
            identity=self.identity,
            origin=self.origin,
            canonical_tool_spec=self.canonical_tool_spec,
        )
        if self.semantic_fingerprint != expected:
            raise ValueError("tool capability semantic fingerprint mismatch")

    @property
    def fact_semantic_fingerprint(self) -> str:
        return self.semantic_fingerprint


def tool_capability_semantic_fingerprint(
    *,
    identity: CapabilityIdentity,
    origin: ToolCapabilityOrigin,
    canonical_tool_spec: FrozenToolSpec,
) -> str:
    return context_fingerprint(
        "tool-capability-fact:v1",
        {
            "identity_fingerprint": identity.identity_fingerprint,
            "origin": origin.value,
            "name": canonical_tool_spec.name,
            "description": canonical_tool_spec.description,
            "parameters": canonical_tool_spec.parameters,
            "descriptor_fingerprint": (
                canonical_tool_spec.descriptor_fingerprint
            ),
        },
    )


def freeze_tool_capability_fact(
    *,
    identity: CapabilityIdentity,
    origin: ToolCapabilityOrigin,
    canonical_tool_spec: FrozenToolSpec,
) -> FrozenToolCapabilityFact:
    return FrozenToolCapabilityFact(
        identity=identity,
        origin=origin,
        canonical_tool_spec=canonical_tool_spec,
        semantic_fingerprint=tool_capability_semantic_fingerprint(
            identity=identity,
            origin=origin,
            canonical_tool_spec=canonical_tool_spec,
        ),
    )


@dataclass(frozen=True, slots=True)
class ToolCapabilityVersionRef:
    identity_fingerprint: str
    semantic_fingerprint: str
    provider_name: str
    version_fingerprint: str

    def __post_init__(self) -> None:
        if not all(
            (self.identity_fingerprint, self.semantic_fingerprint, self.provider_name)
        ):
            raise ValueError("tool capability version is incomplete")
        expected = context_fingerprint(
            "tool-capability-version:v1",
            {
                "identity_fingerprint": self.identity_fingerprint,
                "semantic_fingerprint": self.semantic_fingerprint,
                "provider_name": self.provider_name,
            },
        )
        if self.version_fingerprint != expected:
            raise ValueError("tool capability version fingerprint mismatch")


def tool_capability_version_ref(
    fact: FrozenToolCapabilityFact,
) -> ToolCapabilityVersionRef:
    payload = {
        "identity_fingerprint": fact.identity.identity_fingerprint,
        "semantic_fingerprint": fact.fact_semantic_fingerprint,
        "provider_name": fact.canonical_tool_spec.name,
    }
    return ToolCapabilityVersionRef(
        **payload,
        version_fingerprint=context_fingerprint(
            "tool-capability-version:v1", payload
        ),
    )


class NativeToolWireIncompatibilityReason(StrEnum):
    ROOT_SHAPE_UNSUPPORTED = "ROOT_SHAPE_UNSUPPORTED"
    COMPOSITION_UNSUPPORTED = "COMPOSITION_UNSUPPORTED"
    CONSTRAINT_UNSUPPORTED = "CONSTRAINT_UNSUPPORTED"
    PROJECTION_OVERBOUND = "PROJECTION_OVERBOUND"


@dataclass(frozen=True, slots=True)
class FrozenNativeToolWireEligibilityQuote:
    """Lightweight proof that one canonical Tool can be lowered natively."""

    capability_version_fingerprint: str
    canonical_tool_spec_fingerprint: str
    native_function_tool_wire_contract_fingerprint: str
    wire_tool_fingerprint: str
    wire_utf8_bytes: int
    eligibility_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.wire_tool_fingerprint
            or not 0 < self.wire_utf8_bytes <= MAXIMUM_NATIVE_TOOL_BYTES
        ):
            raise ValueError("native tool eligibility quote is invalid")
        expected = context_fingerprint(
            "native-tool-wire-eligibility-quote:v1",
            {
                "capability_version_fingerprint": (
                    self.capability_version_fingerprint
                ),
                "canonical_tool_spec_fingerprint": (
                    self.canonical_tool_spec_fingerprint
                ),
                "native_function_tool_wire_contract_fingerprint": (
                    self.native_function_tool_wire_contract_fingerprint
                ),
                "wire_tool_fingerprint": self.wire_tool_fingerprint,
                "wire_utf8_bytes": self.wire_utf8_bytes,
            },
        )
        if self.eligibility_fingerprint != expected:
            raise ValueError("native tool eligibility quote fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class FrozenNativeToolWireProjection:
    capability_version_fingerprint: str
    canonical_tool_spec_fingerprint: str
    native_function_tool_wire_contract_fingerprint: str
    wire_tool: FrozenJsonObjectFact = field(repr=False)
    projection_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.wire_tool, FrozenJsonObjectFact):
            raise TypeError("native tool wire projection is not frozen JSON")
        expected = context_fingerprint(
            "native-tool-wire-projection:v1",
            {
                "capability_version_fingerprint": (
                    self.capability_version_fingerprint
                ),
                "canonical_tool_spec_fingerprint": (
                    self.canonical_tool_spec_fingerprint
                ),
                "native_function_tool_wire_contract_fingerprint": (
                    self.native_function_tool_wire_contract_fingerprint
                ),
                "wire_tool": self.wire_tool,
            },
        )
        if self.projection_fingerprint != expected:
            raise ValueError("native tool projection fingerprint mismatch")

    @property
    def wire_utf8_bytes(self) -> int:
        return len(canonical_json_bytes(self.wire_tool))


@dataclass(frozen=True, slots=True)
class FrozenNativeToolWireIncompatibility:
    capability_version_fingerprint: str
    canonical_tool_spec_fingerprint: str
    native_function_tool_wire_contract_fingerprint: str
    reason: NativeToolWireIncompatibilityReason
    decision_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, NativeToolWireIncompatibilityReason):
            raise TypeError("native tool incompatibility reason is not closed")
        expected = context_fingerprint(
            "native-tool-wire-incompatibility:v1",
            {
                "capability_version_fingerprint": (
                    self.capability_version_fingerprint
                ),
                "canonical_tool_spec_fingerprint": (
                    self.canonical_tool_spec_fingerprint
                ),
                "native_function_tool_wire_contract_fingerprint": (
                    self.native_function_tool_wire_contract_fingerprint
                ),
                "reason": self.reason.value,
            },
        )
        if self.decision_fingerprint != expected:
            raise ValueError("native tool incompatibility fingerprint mismatch")


NativeToolWireEligibility = (
    FrozenNativeToolWireEligibilityQuote | FrozenNativeToolWireIncompatibility
)


@dataclass(frozen=True, slots=True)
class FrozenNativeToolWireEligibilitySet:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    native_function_tool_wire_contract_fingerprint: str
    entries: tuple[NativeToolWireEligibility, ...]
    eligibility_set_fingerprint: str

    def __post_init__(self) -> None:
        _validate_scope(
            self.conversation_scope_kind, self.scope_subagent_task_id
        )
        versions = tuple(item.capability_version_fingerprint for item in self.entries)
        if versions != tuple(sorted(versions)) or len(versions) != len(set(versions)):
            raise ValueError("native eligibility entries are not sorted and unique")
        if any(
            item.native_function_tool_wire_contract_fingerprint
            != self.native_function_tool_wire_contract_fingerprint
            for item in self.entries
        ):
            raise ValueError("native eligibility contract drifted")
        expected = context_fingerprint(
            "native-tool-wire-eligibility-set:v1",
            {
                "scope": self.conversation_scope_kind.value,
                "scope_subagent_task_id": self.scope_subagent_task_id,
                "contract": self.native_function_tool_wire_contract_fingerprint,
                "entries": tuple(
                    item.eligibility_fingerprint
                    if isinstance(item, FrozenNativeToolWireEligibilityQuote)
                    else item.decision_fingerprint
                    for item in self.entries
                ),
            },
        )
        if self.eligibility_set_fingerprint != expected:
            raise ValueError("native eligibility set fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class FrozenNativeToolProjectionSet:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    native_function_tool_wire_contract_fingerprint: str
    tool_versions: tuple[ToolCapabilityVersionRef, ...]
    projections: tuple[FrozenNativeToolWireProjection, ...] = field(repr=False)
    projection_set_fingerprint: str

    def __post_init__(self) -> None:
        _validate_scope(
            self.conversation_scope_kind, self.scope_subagent_task_id
        )
        if len(self.tool_versions) != len(self.projections):
            raise ValueError("native projection set pair count conflicts")
        names = tuple(item.provider_name for item in self.tool_versions)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("native projection tools are not sorted and unique")
        for version, projection in zip(
            self.tool_versions, self.projections, strict=True
        ):
            if (
                version.version_fingerprint
                != projection.capability_version_fingerprint
                or projection.native_function_tool_wire_contract_fingerprint
                != self.native_function_tool_wire_contract_fingerprint
            ):
                raise ValueError("native projection does not exact-join version")
        if len(self.tool_versions) > MAXIMUM_NATIVE_TOOL_COUNT:
            raise ValueError("native tool count bound exceeded")
        if sum(item.wire_utf8_bytes for item in self.projections) > (
            MAXIMUM_NATIVE_TOOL_BYTES
        ):
            raise ValueError("native tool wire byte bound exceeded")
        expected = native_tool_projection_set_fingerprint(
            conversation_scope_kind=self.conversation_scope_kind,
            scope_subagent_task_id=self.scope_subagent_task_id,
            native_function_tool_wire_contract_fingerprint=(
                self.native_function_tool_wire_contract_fingerprint
            ),
            tool_versions=self.tool_versions,
            projections=self.projections,
        )
        if self.projection_set_fingerprint != expected:
            raise ValueError("native projection set fingerprint mismatch")


def native_tool_projection_set_fingerprint(
    *,
    conversation_scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    native_function_tool_wire_contract_fingerprint: str,
    tool_versions: tuple[ToolCapabilityVersionRef, ...],
    projections: tuple[FrozenNativeToolWireProjection, ...],
) -> str:
    return context_fingerprint(
        "native-tool-projection-set:v1",
        {
            "scope": conversation_scope_kind.value,
            "scope_subagent_task_id": scope_subagent_task_id,
            "contract": native_function_tool_wire_contract_fingerprint,
            "versions": tuple(item.version_fingerprint for item in tool_versions),
            "projections": tuple(
                item.projection_fingerprint for item in projections
            ),
        },
    )


@dataclass(frozen=True, slots=True)
class FrozenSkillCapabilityFact:
    identity: CapabilityIdentity
    public_name: str
    description: str
    location: str
    winning_root_provenance_fingerprint: str
    catalog_semantic_fingerprint: str
    activation_semantic_fingerprint: str
    fact_semantic_fingerprint: str

    def __post_init__(self) -> None:
        if (
            self.identity.kind is not CapabilityKind.SKILL
            or self.identity.source.kind
            is not CapabilitySourceKind.LOCAL_SKILL_CATALOG
        ):
            raise ValueError("skill capability identity/source matrix conflicts")
        if self.identity.stable_name != self.public_name:
            raise ValueError("skill identity does not join public name")
        expected = skill_capability_fact_fingerprint(
            identity_fingerprint=self.identity.identity_fingerprint,
            catalog_semantic_fingerprint=self.catalog_semantic_fingerprint,
            activation_semantic_fingerprint=self.activation_semantic_fingerprint,
            winning_root_provenance_fingerprint=(
                self.winning_root_provenance_fingerprint
            ),
        )
        if self.fact_semantic_fingerprint != expected:
            raise ValueError("skill capability fact fingerprint mismatch")


def skill_capability_fact_fingerprint(
    *,
    identity_fingerprint: str,
    catalog_semantic_fingerprint: str,
    activation_semantic_fingerprint: str,
    winning_root_provenance_fingerprint: str,
) -> str:
    return context_fingerprint(
        "skill-capability-fact:v1",
        {
            "identity_fingerprint": identity_fingerprint,
            "catalog_semantic_fingerprint": catalog_semantic_fingerprint,
            "activation_semantic_fingerprint": activation_semantic_fingerprint,
            "winning_root_provenance_fingerprint": (
                winning_root_provenance_fingerprint
            ),
        },
    )


class CapabilitySourceSnapshotDisposition(StrEnum):
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


FrozenCapabilityFact = FrozenToolCapabilityFact | FrozenSkillCapabilityFact


@dataclass(frozen=True, slots=True)
class FrozenCapabilitySourceSnapshot:
    registration: FrozenCapabilitySourceRegistration
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    disposition: CapabilitySourceSnapshotDisposition
    facts: tuple[FrozenCapabilityFact, ...]
    source_snapshot_fingerprint: str

    def __post_init__(self) -> None:
        _validate_scope(
            self.conversation_scope_kind, self.scope_subagent_task_id
        )
        if not isinstance(self.disposition, CapabilitySourceSnapshotDisposition):
            raise TypeError("capability source disposition is not closed")
        if (
            self.disposition is CapabilitySourceSnapshotDisposition.UNAVAILABLE
            and self.facts
        ):
            raise ValueError("unavailable capability source contains facts")
        fact_keys = tuple(
            (
                fact.identity.kind.value,
                fact.identity.identity_fingerprint,
                fact.fact_semantic_fingerprint,
            )
            for fact in self.facts
        )
        if fact_keys != tuple(sorted(fact_keys)):
            raise ValueError("capability source facts are not deterministically sorted")
        if len({item.identity.identity_fingerprint for item in self.facts}) != len(
            self.facts
        ):
            raise ValueError("capability source identities are duplicated")
        for fact in self.facts:
            if fact.identity.source != self.registration.source:
                raise ValueError("capability fact escaped source registration")
            _validate_source_fact_matrix(self.registration.source.kind, fact)
        expected = capability_source_snapshot_fingerprint(
            registration=self.registration,
            conversation_scope_kind=self.conversation_scope_kind,
            scope_subagent_task_id=self.scope_subagent_task_id,
            disposition=self.disposition,
            facts=self.facts,
        )
        if self.source_snapshot_fingerprint != expected:
            raise ValueError("capability source snapshot fingerprint mismatch")


def _validate_source_fact_matrix(
    source_kind: CapabilitySourceKind, fact: FrozenCapabilityFact
) -> None:
    if source_kind is CapabilitySourceKind.BUILTIN_REGISTRY:
        valid = (
            isinstance(fact, FrozenToolCapabilityFact)
            and fact.origin is ToolCapabilityOrigin.BUILTIN
        )
    elif source_kind is CapabilitySourceKind.MCP_SERVER:
        valid = (
            isinstance(fact, FrozenToolCapabilityFact)
            and fact.origin is ToolCapabilityOrigin.MCP
        )
    else:
        valid = isinstance(fact, FrozenSkillCapabilityFact)
    if not valid:
        raise ValueError("capability source/fact closed matrix conflicts")


def capability_source_snapshot_fingerprint(
    *,
    registration: FrozenCapabilitySourceRegistration,
    conversation_scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    disposition: CapabilitySourceSnapshotDisposition,
    facts: tuple[FrozenCapabilityFact, ...],
) -> str:
    return context_fingerprint(
        "capability-source-snapshot:v1",
        {
            "registration": registration.registration_fingerprint,
            "scope": conversation_scope_kind.value,
            "scope_subagent_task_id": scope_subagent_task_id,
            "disposition": disposition.value,
            "facts": tuple(item.fact_semantic_fingerprint for item in facts),
        },
    )


def freeze_capability_source_snapshot(
    *,
    registration: FrozenCapabilitySourceRegistration,
    conversation_scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    disposition: CapabilitySourceSnapshotDisposition,
    facts: tuple[FrozenCapabilityFact, ...],
) -> FrozenCapabilitySourceSnapshot:
    ordered = tuple(
        sorted(
            facts,
            key=lambda fact: (
                fact.identity.kind.value,
                fact.identity.identity_fingerprint,
                fact.fact_semantic_fingerprint,
            ),
        )
    )
    return FrozenCapabilitySourceSnapshot(
        registration=registration,
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        disposition=disposition,
        facts=ordered,
        source_snapshot_fingerprint=capability_source_snapshot_fingerprint(
            registration=registration,
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            disposition=disposition,
            facts=ordered,
        ),
    )


@dataclass(frozen=True, slots=True)
class FrozenCapabilityRegistrySnapshot:
    registration_set: FrozenCapabilitySourceRegistrationSet
    source_snapshots: tuple[FrozenCapabilitySourceSnapshot, ...]
    registry_fingerprint: str

    def __post_init__(self) -> None:
        expected_sources = tuple(
            item.source.source_identity_fingerprint
            for item in self.registration_set.registrations
        )
        actual_sources = tuple(
            item.registration.source.source_identity_fingerprint
            for item in self.source_snapshots
        )
        if actual_sources != expected_sources:
            raise ValueError("registry source coverage is not exact")
        identities: set[str] = set()
        provider_names: set[str] = set()
        for snapshot in self.source_snapshots:
            if (
                snapshot.conversation_scope_kind
                is not self.registration_set.conversation_scope_kind
                or snapshot.scope_subagent_task_id
                != self.registration_set.scope_subagent_task_id
            ):
                raise ValueError("registry source snapshot scope conflicts")
            for fact in snapshot.facts:
                if fact.identity.identity_fingerprint in identities:
                    raise ValueError("registry capability identity is duplicated")
                identities.add(fact.identity.identity_fingerprint)
                if isinstance(fact, FrozenToolCapabilityFact):
                    if fact.canonical_tool_spec.name in provider_names:
                        raise ValueError("registry provider tool name is duplicated")
                    provider_names.add(fact.canonical_tool_spec.name)
        expected = capability_registry_fingerprint(
            registration_set=self.registration_set,
            source_snapshots=self.source_snapshots,
        )
        if self.registry_fingerprint != expected:
            raise ValueError("capability registry fingerprint mismatch")

    @property
    def tool_facts(self) -> tuple[FrozenToolCapabilityFact, ...]:
        return tuple(
            fact
            for snapshot in self.source_snapshots
            for fact in snapshot.facts
            if isinstance(fact, FrozenToolCapabilityFact)
        )

    @property
    def skill_facts(self) -> tuple[FrozenSkillCapabilityFact, ...]:
        return tuple(
            fact
            for snapshot in self.source_snapshots
            for fact in snapshot.facts
            if isinstance(fact, FrozenSkillCapabilityFact)
        )


def capability_registry_fingerprint(
    *,
    registration_set: FrozenCapabilitySourceRegistrationSet,
    source_snapshots: tuple[FrozenCapabilitySourceSnapshot, ...],
) -> str:
    return context_fingerprint(
        "capability-registry-snapshot:v1",
        {
            "registration_set": registration_set.registration_set_fingerprint,
            "source_snapshots": tuple(
                item.source_snapshot_fingerprint for item in source_snapshots
            ),
        },
    )


class CapabilityRouteReasonCode(StrEnum):
    DIRECT_NATIVE_SURFACE = "DIRECT_NATIVE_SURFACE"
    NEW_NOT_IN_NATIVE_SURFACE = "NEW_NOT_IN_NATIVE_SURFACE"
    NEW_COLD_COHORT_META_FALLBACK = "NEW_COLD_COHORT_META_FALLBACK"
    NATIVE_WIRE_INCOMPATIBLE = "NATIVE_WIRE_INCOMPATIBLE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SCOPE_INVISIBLE = "SCOPE_INVISIBLE"
    DIRTY_FENCED = "DIRTY_FENCED"
    SCHEMA_REPLACED = "SCHEMA_REPLACED"
    REMOVED = "REMOVED"
    UNKNOWN_TARGET = "UNKNOWN_TARGET"
    AMBIGUOUS_LEGACY_TARGET = "AMBIGUOUS_LEGACY_TARGET"
    DESCRIPTOR_OVERBOUND = "DESCRIPTOR_OVERBOUND"


@dataclass(frozen=True, slots=True)
class McpToolCapabilityRef:
    server_id: str
    remote_tool_name: str

    def __post_init__(self) -> None:
        if not self.server_id or not self.remote_tool_name:
            raise ValueError("MCP capability target is incomplete")


class ToolCapabilityRouteKind(StrEnum):
    DIRECT = "DIRECT"
    NEW_MCP_META_ONLY = "NEW_MCP_META_ONLY"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class FrozenMcpToolExposure:
    target: McpToolCapabilityRef
    version: ToolCapabilityVersionRef
    route: ToolCapabilityRouteKind
    public_reason_code: CapabilityRouteReasonCode
    route_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.route, ToolCapabilityRouteKind) or not isinstance(
            self.public_reason_code, CapabilityRouteReasonCode
        ):
            raise TypeError("MCP route vocabulary is not closed")
        expected = context_fingerprint(
            "mcp-tool-exposure-route:v1",
            {
                "server_id": self.target.server_id,
                "remote_tool_name": self.target.remote_tool_name,
                "version": self.version.version_fingerprint,
                "route": self.route.value,
                "reason": self.public_reason_code.value,
            },
        )
        if self.route_fingerprint != expected:
            raise ValueError("MCP tool route fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class FrozenMcpRouteProjection:
    routes: tuple[FrozenMcpToolExposure, ...]
    joined_catalog_semantic_fingerprint: str
    projection_fingerprint: str

    def __post_init__(self) -> None:
        keys = tuple((item.target.server_id, item.target.remote_tool_name) for item in self.routes)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("MCP routes are not sorted and unique")
        expected = context_fingerprint(
            "mcp-route-projection:v1",
            {
                "routes": tuple(item.route_fingerprint for item in self.routes),
                "catalog": self.joined_catalog_semantic_fingerprint,
            },
        )
        if self.projection_fingerprint != expected:
            raise ValueError("MCP route projection fingerprint mismatch")


class McpInspectEffectKind(StrEnum):
    READ_ONLY = "READ_ONLY"
    EXTERNAL_EFFECT = "EXTERNAL_EFFECT"


class McpInspectDeliveryDisposition(StrEnum):
    FULL_ELIGIBLE = "FULL_ELIGIBLE"
    DESCRIPTOR_OVERBOUND = "DESCRIPTOR_OVERBOUND"


@dataclass(frozen=True, slots=True)
class FrozenMcpInspectabilityFact:
    """Owner-issued, schema-free proof that one exact inspect DTO fits.

    The MCP owner quotes the complete provider-neutral descriptor while it owns
    the input/output schemas and execution policy.  The generic planner only
    receives the mechanical quote and fingerprints; it never receives a second
    schema copy or physical executor authority.
    """

    target: McpToolCapabilityRef
    version: ToolCapabilityVersionRef
    descriptor_payload_fingerprint: str
    mcp_execution_policy_fingerprint: str
    effect_kind: McpInspectEffectKind
    conservative_logical_utf8_bytes: int
    disposition: McpInspectDeliveryDisposition
    fact_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.effect_kind, McpInspectEffectKind):
            raise TypeError("MCP inspect effect kind is not closed")
        if not isinstance(self.disposition, McpInspectDeliveryDisposition):
            raise TypeError("MCP inspect disposition is not closed")
        if (
            not self.descriptor_payload_fingerprint
            or not self.mcp_execution_policy_fingerprint
            or self.conservative_logical_utf8_bytes < 0
        ):
            raise ValueError("MCP inspectability fact is incomplete")
        fits = (
            self.conservative_logical_utf8_bytes
            <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
        )
        if fits != (
            self.disposition is McpInspectDeliveryDisposition.FULL_ELIGIBLE
        ):
            raise ValueError("MCP inspectability disposition conflicts with quote")
        expected = context_fingerprint(
            "mcp-inspectability-fact:v1",
            {
                "server_id": self.target.server_id,
                "remote_tool_name": self.target.remote_tool_name,
                "version": self.version.version_fingerprint,
                "descriptor_payload": self.descriptor_payload_fingerprint,
                "execution_policy": self.mcp_execution_policy_fingerprint,
                "effect_kind": self.effect_kind.value,
                "logical_utf8_bytes": self.conservative_logical_utf8_bytes,
                "disposition": self.disposition.value,
            },
        )
        if self.fact_fingerprint != expected:
            raise ValueError("MCP inspectability fingerprint mismatch")


def freeze_mcp_inspectability_fact(
    *,
    target: McpToolCapabilityRef,
    version: ToolCapabilityVersionRef,
    descriptor_payload_fingerprint: str,
    mcp_execution_policy_fingerprint: str,
    effect_kind: McpInspectEffectKind,
    conservative_logical_utf8_bytes: int,
) -> FrozenMcpInspectabilityFact:
    disposition = (
        McpInspectDeliveryDisposition.FULL_ELIGIBLE
        if conservative_logical_utf8_bytes
        <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
        else McpInspectDeliveryDisposition.DESCRIPTOR_OVERBOUND
    )
    payload = {
        "server_id": target.server_id,
        "remote_tool_name": target.remote_tool_name,
        "version": version.version_fingerprint,
        "descriptor_payload": descriptor_payload_fingerprint,
        "execution_policy": mcp_execution_policy_fingerprint,
        "effect_kind": effect_kind.value,
        "logical_utf8_bytes": conservative_logical_utf8_bytes,
        "disposition": disposition.value,
    }
    return FrozenMcpInspectabilityFact(
        target=target,
        version=version,
        descriptor_payload_fingerprint=descriptor_payload_fingerprint,
        mcp_execution_policy_fingerprint=mcp_execution_policy_fingerprint,
        effect_kind=effect_kind,
        conservative_logical_utf8_bytes=conservative_logical_utf8_bytes,
        disposition=disposition,
        fact_fingerprint=context_fingerprint(
            "mcp-inspectability-fact:v1", payload
        ),
    )


@dataclass(frozen=True, slots=True)
class FrozenMcpCapabilityProjectionInput:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    source_snapshot_fingerprints: tuple[str, ...]
    catalog_semantic_fingerprint: str
    inspectability_facts: tuple[FrozenMcpInspectabilityFact, ...]
    projection_fingerprint: str

    def __post_init__(self) -> None:
        _validate_scope(
            self.conversation_scope_kind, self.scope_subagent_task_id
        )
        if self.source_snapshot_fingerprints != tuple(
            sorted(self.source_snapshot_fingerprints)
        ) or len(self.source_snapshot_fingerprints) != len(
            set(self.source_snapshot_fingerprints)
        ):
            raise ValueError("MCP source snapshot refs are not sorted and unique")
        inspect_keys = tuple(
            (item.target.server_id, item.target.remote_tool_name)
            for item in self.inspectability_facts
        )
        if inspect_keys != tuple(sorted(inspect_keys)) or len(inspect_keys) != len(
            set(inspect_keys)
        ):
            raise ValueError("MCP inspectability facts are not sorted and unique")
        expected = context_fingerprint(
            "mcp-capability-projection-input:v1",
            {
                "scope": self.conversation_scope_kind.value,
                "scope_subagent_task_id": self.scope_subagent_task_id,
                "sources": self.source_snapshot_fingerprints,
                "catalog": self.catalog_semantic_fingerprint,
                "inspectability": tuple(
                    item.fact_fingerprint for item in self.inspectability_facts
                ),
            },
        )
        if self.projection_fingerprint != expected:
            raise ValueError("MCP projection input fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class FrozenSkillProjectionInput:
    discovery_semantic_fingerprint: str
    source_snapshot_fingerprint: str
    snapshot_fingerprint: str

    def __post_init__(self) -> None:
        if not self.discovery_semantic_fingerprint:
            raise ValueError("skill discovery semantic fingerprint is empty")
        if not self.source_snapshot_fingerprint:
            raise ValueError("skill source snapshot fingerprint is empty")
        expected = skill_projection_input_fingerprint(
            discovery_semantic_fingerprint=self.discovery_semantic_fingerprint,
            source_snapshot_fingerprint=self.source_snapshot_fingerprint,
        )
        if self.snapshot_fingerprint != expected:
            raise ValueError("skill projection input fingerprint mismatch")


def skill_projection_input_fingerprint(
    *,
    discovery_semantic_fingerprint: str,
    source_snapshot_fingerprint: str,
) -> str:
    return context_fingerprint(
        "frozen-skill-projection-input:v2-agent-skills",
        {
            "source_snapshot": source_snapshot_fingerprint,
            "discovery_semantic_fingerprint": discovery_semantic_fingerprint,
        },
    )


@dataclass(frozen=True, slots=True)
class EmptyCapabilityEpochPredecessor:
    expected_continuity_revision: Literal[0]

    def __post_init__(self) -> None:
        if self.expected_continuity_revision != 0:
            raise ValueError("empty capability predecessor revision is not zero")


@dataclass(frozen=True, slots=True)
class InstalledCapabilityEpochPredecessor:
    expected_continuity_revision: int
    continuity_epoch_nonce: str
    tool_surface: FrozenModelToolSurface
    direct_projection_set: FrozenNativeToolProjectionSet
    mcp_route_projection: FrozenMcpRouteProjection

    def __post_init__(self) -> None:
        if self.expected_continuity_revision < 1 or not self.continuity_epoch_nonce:
            raise ValueError("installed capability predecessor is incomplete")
        if (
            self.tool_surface.conversation_scope_kind
            is not self.direct_projection_set.conversation_scope_kind
        ):
            raise ValueError("installed capability predecessor scope conflicts")
        if tuple(item.name for item in self.tool_surface.tool_specs) != tuple(
            item.provider_name for item in self.direct_projection_set.tool_versions
        ):
            raise ValueError("installed capability projection/surface drifted")


CapabilityEpochPredecessor = (
    EmptyCapabilityEpochPredecessor | InstalledCapabilityEpochPredecessor
)


@dataclass(frozen=True, slots=True)
class FrozenToolCapabilityPlanningInput:
    predecessor: CapabilityEpochPredecessor
    native_wire: FrozenNativeToolWireEligibilitySet
    mcp: FrozenMcpCapabilityProjectionInput
    tool_view_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(
            self.predecessor,
            (EmptyCapabilityEpochPredecessor, InstalledCapabilityEpochPredecessor),
        ):
            raise TypeError("capability predecessor union is open")
        if (
            self.native_wire.conversation_scope_kind
            is not self.mcp.conversation_scope_kind
            or self.native_wire.scope_subagent_task_id
            != self.mcp.scope_subagent_task_id
        ):
            raise ValueError("Tool planning input scopes conflict")
        if isinstance(self.predecessor, InstalledCapabilityEpochPredecessor) and (
            self.predecessor.direct_projection_set.conversation_scope_kind
            is not self.native_wire.conversation_scope_kind
            or self.predecessor.direct_projection_set.scope_subagent_task_id
            != self.native_wire.scope_subagent_task_id
        ):
            raise ValueError("installed Tool predecessor scope conflicts")
        if self.tool_view_fingerprint != tool_capability_planning_input_fingerprint(
            predecessor=self.predecessor,
            native_wire=self.native_wire,
            mcp=self.mcp,
        ):
            raise ValueError("Tool planning input fingerprint mismatch")


def tool_capability_planning_input_fingerprint(
    *,
    predecessor: CapabilityEpochPredecessor,
    native_wire: FrozenNativeToolWireEligibilitySet,
    mcp: FrozenMcpCapabilityProjectionInput,
) -> str:
    return context_fingerprint(
        "tool-capability-planning-input:v1",
        {
            "predecessor": (
                {"kind": "EMPTY", "revision": 0}
                if isinstance(predecessor, EmptyCapabilityEpochPredecessor)
                else {
                    "kind": "INSTALLED",
                    "revision": predecessor.expected_continuity_revision,
                    "epoch_nonce": predecessor.continuity_epoch_nonce,
                    "tool_surface": predecessor.tool_surface.surface_fingerprint,
                    "projection_set": (
                        predecessor.direct_projection_set.projection_set_fingerprint
                    ),
                    "mcp_routes": (
                        predecessor.mcp_route_projection.projection_fingerprint
                    ),
                }
            ),
            "native_wire": native_wire.eligibility_set_fingerprint,
            "mcp": mcp.projection_fingerprint,
        },
    )


@dataclass(frozen=True, slots=True)
class FrozenCapabilityDispatchCut:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    registry: FrozenCapabilityRegistrySnapshot
    tools: FrozenToolCapabilityPlanningInput
    skills: FrozenSkillProjectionInput
    dispatch_cut_fingerprint: str

    def __post_init__(self) -> None:
        _validate_scope(
            self.conversation_scope_kind, self.scope_subagent_task_id
        )
        registration = self.registry.registration_set
        if (
            registration.conversation_scope_kind
            is not self.conversation_scope_kind
            or registration.scope_subagent_task_id != self.scope_subagent_task_id
            or self.tools.native_wire.conversation_scope_kind
            is not self.conversation_scope_kind
            or self.tools.native_wire.scope_subagent_task_id
            != self.scope_subagent_task_id
        ):
            raise ValueError("capability dispatch cut scopes conflict")
        expected = capability_dispatch_cut_fingerprint(
            conversation_scope_kind=self.conversation_scope_kind,
            scope_subagent_task_id=self.scope_subagent_task_id,
            registry=self.registry,
            tools=self.tools,
            skills=self.skills,
        )
        if self.dispatch_cut_fingerprint != expected:
            raise ValueError("capability dispatch cut fingerprint mismatch")


def capability_dispatch_cut_fingerprint(
    *,
    conversation_scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    registry: FrozenCapabilityRegistrySnapshot,
    tools: FrozenToolCapabilityPlanningInput,
    skills: FrozenSkillProjectionInput,
) -> str:
    return context_fingerprint(
        "capability-dispatch-cut:v1",
        {
            "scope": conversation_scope_kind.value,
            "scope_subagent_task_id": scope_subagent_task_id,
            "registry": registry.registry_fingerprint,
            "tools": tools.tool_view_fingerprint,
            "skills": skills.snapshot_fingerprint,
        },
    )


@dataclass(frozen=True, slots=True)
class FrozenToolCapabilityDispatchView:
    parent_dispatch_cut_fingerprint: str
    registry_fingerprint: str
    registry_tool_facts: tuple[FrozenToolCapabilityFact, ...]
    planning_input: FrozenToolCapabilityPlanningInput
    view_fingerprint: str

    def __post_init__(self) -> None:
        keys = tuple(
            (item.identity.identity_fingerprint, item.fact_semantic_fingerprint)
            for item in self.registry_tool_facts
        )
        if len(keys) != len(set(keys)):
            raise ValueError("Tool dispatch facts are not unique")
        expected = tool_capability_dispatch_view_fingerprint(
            parent_dispatch_cut_fingerprint=self.parent_dispatch_cut_fingerprint,
            registry_fingerprint=self.registry_fingerprint,
            registry_tool_facts=self.registry_tool_facts,
            planning_input=self.planning_input,
        )
        if self.view_fingerprint != expected:
            raise ValueError("Tool dispatch view fingerprint mismatch")


def tool_capability_dispatch_view_fingerprint(
    *,
    parent_dispatch_cut_fingerprint: str,
    registry_fingerprint: str,
    registry_tool_facts: tuple[FrozenToolCapabilityFact, ...],
    planning_input: FrozenToolCapabilityPlanningInput,
) -> str:
    return context_fingerprint(
        "tool-capability-dispatch-view:v1",
        {
            "parent": parent_dispatch_cut_fingerprint,
            "registry": registry_fingerprint,
            "facts": tuple(
                item.fact_semantic_fingerprint for item in registry_tool_facts
            ),
            "planning": planning_input.tool_view_fingerprint,
        },
    )


@dataclass(frozen=True, slots=True)
class FrozenSkillCapabilityDispatchView:
    parent_dispatch_cut_fingerprint: str
    registry_fingerprint: str
    registry_skill_facts: tuple[FrozenSkillCapabilityFact, ...]
    projection_input: FrozenSkillProjectionInput
    view_fingerprint: str

    def __post_init__(self) -> None:
        keys = tuple(
            (item.identity.identity_fingerprint, item.fact_semantic_fingerprint)
            for item in self.registry_skill_facts
        )
        if len(keys) != len(set(keys)):
            raise ValueError("Skill dispatch facts are not unique")
        expected = skill_capability_dispatch_view_fingerprint(
            parent_dispatch_cut_fingerprint=self.parent_dispatch_cut_fingerprint,
            registry_fingerprint=self.registry_fingerprint,
            registry_skill_facts=self.registry_skill_facts,
            projection_input=self.projection_input,
        )
        if self.view_fingerprint != expected:
            raise ValueError("Skill dispatch view fingerprint mismatch")


def skill_capability_dispatch_view_fingerprint(
    *,
    parent_dispatch_cut_fingerprint: str,
    registry_fingerprint: str,
    registry_skill_facts: tuple[FrozenSkillCapabilityFact, ...],
    projection_input: FrozenSkillProjectionInput,
) -> str:
    return context_fingerprint(
        "skill-capability-dispatch-view:v1",
        {
            "parent": parent_dispatch_cut_fingerprint,
            "registry": registry_fingerprint,
            "facts": tuple(
                item.fact_semantic_fingerprint for item in registry_skill_facts
            ),
            "projection": projection_input.snapshot_fingerprint,
        },
    )


@dataclass(frozen=True, slots=True)
class FrozenToolCapabilityExposureSelection:
    """Pure planner output before selected native wire is materialized."""

    dispatch_cut_fingerprint: str
    tool_dispatch_view_fingerprint: str
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    native_function_tool_wire_contract_fingerprint: str
    direct_tool_surface: FrozenModelToolSurface
    direct_tool_versions: tuple[ToolCapabilityVersionRef, ...]
    mcp_catalog_route_projection: FrozenMcpRouteProjection
    selection_fingerprint: str
    reusable_direct_projection_set: FrozenNativeToolProjectionSet | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        _validate_scope(
            self.conversation_scope_kind, self.scope_subagent_task_id
        )
        if (
            self.direct_tool_surface.conversation_scope_kind
            is not self.conversation_scope_kind
        ):
            raise ValueError("capability selection Tool surface scope conflicts")
        if tuple(item.name for item in self.direct_tool_surface.tool_specs) != tuple(
            item.provider_name for item in self.direct_tool_versions
        ):
            raise ValueError("capability selection Tool versions drifted")
        reusable = self.reusable_direct_projection_set
        if reusable is not None and (
            reusable.conversation_scope_kind is not self.conversation_scope_kind
            or reusable.scope_subagent_task_id != self.scope_subagent_task_id
            or reusable.native_function_tool_wire_contract_fingerprint
            != self.native_function_tool_wire_contract_fingerprint
            or reusable.tool_versions != self.direct_tool_versions
        ):
            raise ValueError("reusable native projection set does not join selection")
        expected = tool_capability_exposure_selection_fingerprint(
            dispatch_cut_fingerprint=self.dispatch_cut_fingerprint,
            tool_dispatch_view_fingerprint=self.tool_dispatch_view_fingerprint,
            conversation_scope_kind=self.conversation_scope_kind,
            scope_subagent_task_id=self.scope_subagent_task_id,
            native_function_tool_wire_contract_fingerprint=(
                self.native_function_tool_wire_contract_fingerprint
            ),
            direct_tool_surface=self.direct_tool_surface,
            direct_tool_versions=self.direct_tool_versions,
            reusable_direct_projection_set=reusable,
            mcp_catalog_route_projection=self.mcp_catalog_route_projection,
        )
        if self.selection_fingerprint != expected:
            raise ValueError("capability exposure selection fingerprint mismatch")


def tool_capability_exposure_selection_fingerprint(
    *,
    dispatch_cut_fingerprint: str,
    tool_dispatch_view_fingerprint: str,
    conversation_scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    native_function_tool_wire_contract_fingerprint: str,
    direct_tool_surface: FrozenModelToolSurface,
    direct_tool_versions: tuple[ToolCapabilityVersionRef, ...],
    reusable_direct_projection_set: FrozenNativeToolProjectionSet | None,
    mcp_catalog_route_projection: FrozenMcpRouteProjection,
) -> str:
    return context_fingerprint(
        "tool-capability-exposure-selection:v1",
        {
            "dispatch_cut": dispatch_cut_fingerprint,
            "view": tool_dispatch_view_fingerprint,
            "scope": conversation_scope_kind.value,
            "scope_subagent_task_id": scope_subagent_task_id,
            "contract": native_function_tool_wire_contract_fingerprint,
            "surface": direct_tool_surface.surface_fingerprint,
            "versions": tuple(
                item.version_fingerprint for item in direct_tool_versions
            ),
            "reusable_projections": (
                reusable_direct_projection_set.projection_set_fingerprint
                if reusable_direct_projection_set is not None
                else None
            ),
            "mcp_routes": mcp_catalog_route_projection.projection_fingerprint,
        },
    )


@dataclass(frozen=True, slots=True)
class FrozenToolCapabilityExposurePlan:
    dispatch_cut_fingerprint: str
    tool_dispatch_view_fingerprint: str
    direct_tool_surface: FrozenModelToolSurface
    direct_projection_set: FrozenNativeToolProjectionSet
    mcp_catalog_route_projection: FrozenMcpRouteProjection
    exposure_plan_fingerprint: str

    def __post_init__(self) -> None:
        if tuple(item.name for item in self.direct_tool_surface.tool_specs) != tuple(
            item.provider_name for item in self.direct_projection_set.tool_versions
        ):
            raise ValueError("capability exposure surface/projections drifted")
        if (
            self.direct_tool_surface.conversation_scope_kind
            is not self.direct_projection_set.conversation_scope_kind
        ):
            raise ValueError("capability exposure plan scopes conflict")
        direct_versions = {
            item.version_fingerprint
            for item in self.direct_projection_set.tool_versions
        }
        if any(
            (
                route.route is ToolCapabilityRouteKind.DIRECT
                and route.version.version_fingerprint not in direct_versions
            )
            or (
                route.route is ToolCapabilityRouteKind.NEW_MCP_META_ONLY
                and route.version.version_fingerprint in direct_versions
            )
            for route in self.mcp_catalog_route_projection.routes
        ):
            raise ValueError("MCP route conflicts with the selected native cohort")
        expected = tool_capability_exposure_plan_fingerprint(
            dispatch_cut_fingerprint=self.dispatch_cut_fingerprint,
            tool_dispatch_view_fingerprint=self.tool_dispatch_view_fingerprint,
            direct_tool_surface=self.direct_tool_surface,
            direct_projection_set=self.direct_projection_set,
            mcp_catalog_route_projection=self.mcp_catalog_route_projection,
        )
        if self.exposure_plan_fingerprint != expected:
            raise ValueError("capability exposure plan fingerprint mismatch")


def tool_capability_exposure_plan_fingerprint(
    *,
    dispatch_cut_fingerprint: str,
    tool_dispatch_view_fingerprint: str,
    direct_tool_surface: FrozenModelToolSurface,
    direct_projection_set: FrozenNativeToolProjectionSet,
    mcp_catalog_route_projection: FrozenMcpRouteProjection,
) -> str:
    return context_fingerprint(
        "tool-capability-exposure-plan:v1",
        {
            "dispatch_cut": dispatch_cut_fingerprint,
            "view": tool_dispatch_view_fingerprint,
            "surface": direct_tool_surface.surface_fingerprint,
            "projections": direct_projection_set.projection_set_fingerprint,
            "mcp_routes": mcp_catalog_route_projection.projection_fingerprint,
        },
    )


@dataclass(frozen=True, slots=True)
class PreparedUnavailableDirectMcpGate:
    capability_identity_fingerprint: str
    tool_semantic_fingerprint: str
    provider_tool_name: str
    unavailable_reason_code: str
    supervisor_authority_identity: object = field(repr=False, compare=False)
    gate_fingerprint: str = ""

    def __post_init__(self) -> None:
        expected = context_fingerprint(
            "unavailable-direct-mcp-gate:v1",
            {
                "identity": self.capability_identity_fingerprint,
                "semantic": self.tool_semantic_fingerprint,
                "provider_name": self.provider_tool_name,
                "reason": self.unavailable_reason_code,
            },
        )
        if self.gate_fingerprint != expected:
            raise ValueError("unavailable direct MCP gate fingerprint mismatch")

    @property
    def tool_name(self) -> str:
        return self.provider_tool_name

    @property
    def descriptor_fingerprint(self) -> str:
        return self.tool_semantic_fingerprint

    @property
    def executor_binding_fingerprint(self) -> str:
        return self.gate_fingerprint


def frozen_tool_spec_fingerprint(spec: FrozenToolSpec) -> str:
    return context_fingerprint(
        "canonical-tool-spec:v1",
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
            "descriptor_fingerprint": spec.descriptor_fingerprint,
        },
    )


def canonical_tool_spec_fingerprint(fact: FrozenToolCapabilityFact) -> str:
    """Public mechanical hash used by adapter eligibility projections."""

    return frozen_tool_spec_fingerprint(fact.canonical_tool_spec)


__all__ = [
    name
    for name in globals()
    if name.startswith(("Capability", "Empty", "Frozen", "Installed", "Local", "Mcp", "Native", "Prepared", "Tool", "MAXIMUM_"))
    or name.startswith(("capability_", "canonical_", "freeze_", "native_", "skill_", "tool_"))
]
