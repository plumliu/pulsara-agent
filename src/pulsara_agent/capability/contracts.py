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
from typing import TYPE_CHECKING, Literal

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

if TYPE_CHECKING:
    from pulsara_agent.capability.resolver import EffectiveSkillCatalogInspection
    from pulsara_agent.capability.types import SkillDefinitionOrigin

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

    def __post_init__(self) -> None:
        if not isinstance(self.source, CapabilitySourceRef):
            raise TypeError("registration source is not frozen")
        if not isinstance(self.refresh_mode, CapabilitySourceRefreshMode):
            raise TypeError("source refresh mode is not closed")
        if not self.source_contract_fingerprint:
            raise ValueError("source contract fingerprint is empty")
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
    return FrozenCapabilitySourceRegistration(
        source=source,
        refresh_mode=refresh_mode,
        source_contract_fingerprint=source_contract_fingerprint,
    )


def capability_source_registration_identity_digest(
    registration: FrozenCapabilitySourceRegistration,
) -> str:
    """Derive the historical source-registration identity at its sole boundary."""

    return context_fingerprint(
        "capability-source-registration:v1",
        {
            "source_identity_fingerprint": (
                registration.source.source_identity_fingerprint
            ),
            "refresh_mode": registration.refresh_mode.value,
            "source_contract_fingerprint": registration.source_contract_fingerprint,
        },
    )


def _validate_scope(scope: ModelInputScopeKind, subagent_task_id: str | None) -> None:
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

    def __post_init__(self) -> None:
        _validate_scope(self.conversation_scope_kind, self.scope_subagent_task_id)
        if (
            self.builtin_registration.source.kind
            is not CapabilitySourceKind.BUILTIN_REGISTRY
            or self.local_skill_catalog_registration.source.kind
            is not CapabilitySourceKind.LOCAL_SKILL_CATALOG
        ):
            raise ValueError("capability registration singleton kinds conflict")
        if len(self.mcp_registrations) > MAXIMUM_CAPABILITY_MCP_SOURCES:
            raise ValueError("MCP source registration bound exceeded")
        mcp_ids = tuple(item.source.stable_source_id for item in self.mcp_registrations)
        if mcp_ids != tuple(sorted(mcp_ids)) or len(mcp_ids) != len(set(mcp_ids)):
            raise ValueError("MCP source registrations are not sorted and unique")
        if any(
            item.source.kind is not CapabilitySourceKind.MCP_SERVER
            for item in self.mcp_registrations
        ):
            raise ValueError("non-MCP source entered MCP registrations")

    @property
    def registrations(self) -> tuple[FrozenCapabilitySourceRegistration, ...]:
        return (
            self.builtin_registration,
            *self.mcp_registrations,
            self.local_skill_catalog_registration,
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
            "descriptor_fingerprint": (canonical_tool_spec.descriptor_fingerprint),
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

    def __post_init__(self) -> None:
        if not all(
            (self.identity_fingerprint, self.semantic_fingerprint, self.provider_name)
        ):
            raise ValueError("tool capability version is incomplete")


def tool_capability_version_identity_digest(
    version: ToolCapabilityVersionRef,
) -> str:
    """Derive the historical prefix identity at its unique wire boundary."""

    return context_fingerprint(
        "tool-capability-version:v1",
        {
            "identity_fingerprint": version.identity_fingerprint,
            "semantic_fingerprint": version.semantic_fingerprint,
            "provider_name": version.provider_name,
        },
    )


def tool_capability_version_ref(
    fact: FrozenToolCapabilityFact,
) -> ToolCapabilityVersionRef:
    return ToolCapabilityVersionRef(
        identity_fingerprint=fact.identity.identity_fingerprint,
        semantic_fingerprint=fact.fact_semantic_fingerprint,
        provider_name=fact.canonical_tool_spec.name,
    )


class NativeToolWireIncompatibilityReason(StrEnum):
    ROOT_SHAPE_UNSUPPORTED = "ROOT_SHAPE_UNSUPPORTED"
    COMPOSITION_UNSUPPORTED = "COMPOSITION_UNSUPPORTED"
    CONSTRAINT_UNSUPPORTED = "CONSTRAINT_UNSUPPORTED"
    PROJECTION_OVERBOUND = "PROJECTION_OVERBOUND"


@dataclass(frozen=True, slots=True)
class FrozenNativeToolWireEligibilityQuote:
    """Lightweight proof that one canonical Tool can be lowered natively."""

    version: ToolCapabilityVersionRef
    canonical_tool_spec_fingerprint: str
    native_function_tool_wire_contract_fingerprint: str
    wire_tool_fingerprint: str
    wire_utf8_bytes: int

    def __post_init__(self) -> None:
        if (
            not self.wire_tool_fingerprint
            or not 0 < self.wire_utf8_bytes <= MAXIMUM_NATIVE_TOOL_BYTES
        ):
            raise ValueError("native tool eligibility quote is invalid")


@dataclass(frozen=True, slots=True)
class FrozenNativeToolWireProjection:
    version: ToolCapabilityVersionRef
    canonical_tool_spec_fingerprint: str
    native_function_tool_wire_contract_fingerprint: str
    wire_tool: FrozenJsonObjectFact = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.wire_tool, FrozenJsonObjectFact):
            raise TypeError("native tool wire projection is not frozen JSON")

    @property
    def wire_utf8_bytes(self) -> int:
        return len(canonical_json_bytes(self.wire_tool))


@dataclass(frozen=True, slots=True)
class FrozenNativeToolWireIncompatibility:
    version: ToolCapabilityVersionRef
    canonical_tool_spec_fingerprint: str
    native_function_tool_wire_contract_fingerprint: str
    reason: NativeToolWireIncompatibilityReason

    def __post_init__(self) -> None:
        if not isinstance(self.reason, NativeToolWireIncompatibilityReason):
            raise TypeError("native tool incompatibility reason is not closed")


NativeToolWireEligibility = (
    FrozenNativeToolWireEligibilityQuote | FrozenNativeToolWireIncompatibility
)


@dataclass(frozen=True, slots=True)
class FrozenNativeToolWireEligibilitySet:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    native_function_tool_wire_contract_fingerprint: str
    entries: tuple[NativeToolWireEligibility, ...]

    def __post_init__(self) -> None:
        _validate_scope(self.conversation_scope_kind, self.scope_subagent_task_id)
        versions = tuple(item.version for item in self.entries)
        keys = tuple(
            (item.provider_name, item.identity_fingerprint, item.semantic_fingerprint)
            for item in versions
        )
        if keys != tuple(sorted(keys)) or len(versions) != len(set(versions)):
            raise ValueError("native eligibility entries are not sorted and unique")
        if any(
            item.native_function_tool_wire_contract_fingerprint
            != self.native_function_tool_wire_contract_fingerprint
            for item in self.entries
        ):
            raise ValueError("native eligibility contract drifted")


@dataclass(frozen=True, slots=True)
class FrozenNativeToolProjectionSet:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    native_function_tool_wire_contract_fingerprint: str
    tool_versions: tuple[ToolCapabilityVersionRef, ...]
    projections: tuple[FrozenNativeToolWireProjection, ...] = field(repr=False)
    projection_set_fingerprint: str

    def __post_init__(self) -> None:
        _validate_scope(self.conversation_scope_kind, self.scope_subagent_task_id)
        if len(self.tool_versions) != len(self.projections):
            raise ValueError("native projection set pair count conflicts")
        names = tuple(item.provider_name for item in self.tool_versions)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("native projection tools are not sorted and unique")
        for version, projection in zip(
            self.tool_versions, self.projections, strict=True
        ):
            if (
                version != projection.version
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
            "versions": tuple(
                tool_capability_version_identity_digest(item) for item in tool_versions
            ),
            "projections": tuple(
                context_fingerprint(
                    "native-tool-wire-projection:v1",
                    {
                        "capability_version_fingerprint": (
                            tool_capability_version_identity_digest(item.version)
                        ),
                        "canonical_tool_spec_fingerprint": (
                            item.canonical_tool_spec_fingerprint
                        ),
                        "native_function_tool_wire_contract_fingerprint": (
                            item.native_function_tool_wire_contract_fingerprint
                        ),
                        "wire_tool": item.wire_tool,
                    },
                )
                for item in projections
            ),
        },
    )


@dataclass(frozen=True, slots=True)
class FrozenSkillCapabilityFact:
    identity: CapabilityIdentity
    public_name: str
    description: str
    location: str
    origin: "SkillDefinitionOrigin"
    catalog_semantic_fingerprint: str
    activation_semantic_fingerprint: str
    fact_semantic_fingerprint: str

    def __post_init__(self) -> None:
        if (
            self.identity.kind is not CapabilityKind.SKILL
            or self.identity.source.kind is not CapabilitySourceKind.LOCAL_SKILL_CATALOG
        ):
            raise ValueError("skill capability identity/source matrix conflicts")
        if self.identity.stable_name != self.public_name:
            raise ValueError("skill identity does not join public name")
        from pulsara_agent.capability.types import (
            BundledSkillOrigin,
            LooseSkillOrigin,
        )

        if not isinstance(self.origin, (LooseSkillOrigin, BundledSkillOrigin)):
            raise TypeError("skill capability fact origin is not closed")
        if isinstance(self.origin, BundledSkillOrigin) and (
            self.origin.skill_name != self.public_name
        ):
            raise ValueError("bundled Skill fact origin/name join conflicts")
        expected = skill_capability_fact_fingerprint(
            identity_fingerprint=self.identity.identity_fingerprint,
            catalog_semantic_fingerprint=self.catalog_semantic_fingerprint,
            activation_semantic_fingerprint=self.activation_semantic_fingerprint,
            origin=self.origin,
        )
        if self.fact_semantic_fingerprint != expected:
            raise ValueError("skill capability fact fingerprint mismatch")


def skill_capability_fact_fingerprint(
    *,
    identity_fingerprint: str,
    catalog_semantic_fingerprint: str,
    activation_semantic_fingerprint: str,
    origin: "SkillDefinitionOrigin",
) -> str:
    origin_digest = _skill_origin_provenance_fingerprint(origin)
    return context_fingerprint(
        "skill-capability-fact:v1",
        {
            "identity_fingerprint": identity_fingerprint,
            "catalog_semantic_fingerprint": catalog_semantic_fingerprint,
            "activation_semantic_fingerprint": activation_semantic_fingerprint,
            "winning_root_provenance_fingerprint": (
                origin_digest
            ),
        },
    )


def _skill_origin_provenance_fingerprint(origin: "SkillDefinitionOrigin") -> str:
    """Derive the historical fact payload slot at its sole real boundary."""

    from pulsara_agent.capability.types import BundledSkillOrigin, LooseSkillOrigin

    if isinstance(origin, LooseSkillOrigin):
        ordinal, prefix = {
            LocalSkillRootKind.WORKSPACE_PULSARA: (0, ".pulsara/skills"),
            LocalSkillRootKind.WORKSPACE_AGENTS: (1, ".agents/skills"),
            LocalSkillRootKind.USER_PULSARA: (2, "${PULSARA_HOME}/skills"),
            LocalSkillRootKind.USER_AGENTS: (3, "~/.agents/skills"),
        }[origin.root_kind]
        return context_fingerprint(
            "local-skill-winning-root-provenance:v2-agent-skills",
            {
                "root_kind": origin.root_kind.value,
                "precedence_ordinal": ordinal,
                "stable_location_prefix": prefix,
            },
        )
    if isinstance(origin, BundledSkillOrigin):
        return context_fingerprint(
            "bundled-skill-origin:v1-agent-skills",
            {
                "package": "pulsara_agent",
                "relative_skill_directory": (
                    origin.package_relative_skill_directory
                ),
            },
        )
    raise TypeError("Skill origin union is open")


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

    def __post_init__(self) -> None:
        _validate_scope(self.conversation_scope_kind, self.scope_subagent_task_id)
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


def capability_source_snapshot_semantic_digest(
    snapshot: FrozenCapabilitySourceSnapshot,
) -> str:
    """Derive source lineage only where a context-source boundary needs it."""

    return context_fingerprint(
        "capability-source-snapshot:v1",
        {
            "registration": capability_source_registration_identity_digest(
                snapshot.registration
            ),
            "scope": snapshot.conversation_scope_kind.value,
            "scope_subagent_task_id": snapshot.scope_subagent_task_id,
            "disposition": snapshot.disposition.value,
            "facts": tuple(item.fact_semantic_fingerprint for item in snapshot.facts),
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
    )


@dataclass(frozen=True, slots=True)
class FrozenCapabilityRegistrySnapshot:
    registration_set: FrozenCapabilitySourceRegistrationSet
    source_snapshots: tuple[FrozenCapabilitySourceSnapshot, ...]

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
                "version": tool_capability_version_identity_digest(self.version),
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
        keys = tuple(
            (item.target.server_id, item.target.remote_tool_name)
            for item in self.routes
        )
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
        if fits != (self.disposition is McpInspectDeliveryDisposition.FULL_ELIGIBLE):
            raise ValueError("MCP inspectability disposition conflicts with quote")


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
    return FrozenMcpInspectabilityFact(
        target=target,
        version=version,
        descriptor_payload_fingerprint=descriptor_payload_fingerprint,
        mcp_execution_policy_fingerprint=mcp_execution_policy_fingerprint,
        effect_kind=effect_kind,
        conservative_logical_utf8_bytes=conservative_logical_utf8_bytes,
        disposition=disposition,
    )


@dataclass(frozen=True, slots=True)
class FrozenMcpCapabilityProjectionInput:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    source_snapshots: tuple[FrozenCapabilitySourceSnapshot, ...] = field(repr=False)
    catalog_semantic_fingerprint: str
    inspectability_facts: tuple[FrozenMcpInspectabilityFact, ...]

    def __post_init__(self) -> None:
        _validate_scope(self.conversation_scope_kind, self.scope_subagent_task_id)
        source_ids = tuple(
            item.registration.source.stable_source_id for item in self.source_snapshots
        )
        if source_ids != tuple(sorted(source_ids)) or len(source_ids) != len(
            set(source_ids)
        ):
            raise ValueError("MCP source snapshots are not sorted and unique")
        if any(
            item.registration.source.kind is not CapabilitySourceKind.MCP_SERVER
            or item.conversation_scope_kind is not self.conversation_scope_kind
            or item.scope_subagent_task_id != self.scope_subagent_task_id
            for item in self.source_snapshots
        ):
            raise ValueError("MCP source snapshot scope or kind conflicts")
        inspect_keys = tuple(
            (item.target.server_id, item.target.remote_tool_name)
            for item in self.inspectability_facts
        )
        if inspect_keys != tuple(sorted(inspect_keys)) or len(inspect_keys) != len(
            set(inspect_keys)
        ):
            raise ValueError("MCP inspectability facts are not sorted and unique")


@dataclass(frozen=True, slots=True)
class FrozenSkillProjectionInput:
    source_snapshot: FrozenCapabilitySourceSnapshot = field(repr=False)
    inspection: "EffectiveSkillCatalogInspection" = field(repr=False)

    def __post_init__(self) -> None:
        from pulsara_agent.capability.resolver import (
            CompleteEffectiveSkillCatalogInspection,
            UnavailableEffectiveSkillCatalogInspection,
        )

        if (
            self.source_snapshot.registration.source.kind
            is not CapabilitySourceKind.LOCAL_SKILL_CATALOG
        ):
            raise ValueError("skill projection input source kind conflicts")
        if not isinstance(
            self.inspection,
            (
                CompleteEffectiveSkillCatalogInspection,
                UnavailableEffectiveSkillCatalogInspection,
            ),
        ):
            raise TypeError("skill projection input inspection is not frozen")


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


@dataclass(frozen=True, slots=True)
class FrozenCapabilityDispatchCut:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    registry: FrozenCapabilityRegistrySnapshot
    tools: FrozenToolCapabilityPlanningInput
    skills: FrozenSkillProjectionInput

    def __post_init__(self) -> None:
        _validate_scope(self.conversation_scope_kind, self.scope_subagent_task_id)
        registration = self.registry.registration_set
        if (
            registration.conversation_scope_kind is not self.conversation_scope_kind
            or registration.scope_subagent_task_id != self.scope_subagent_task_id
            or self.tools.native_wire.conversation_scope_kind
            is not self.conversation_scope_kind
            or self.tools.native_wire.scope_subagent_task_id
            != self.scope_subagent_task_id
        ):
            raise ValueError("capability dispatch cut scopes conflict")


@dataclass(frozen=True, slots=True)
class FrozenToolCapabilityDispatchView:
    parent_dispatch_cut: FrozenCapabilityDispatchCut = field(repr=False)
    registry_tool_facts: tuple[FrozenToolCapabilityFact, ...]

    def __post_init__(self) -> None:
        keys = tuple(
            (item.identity.identity_fingerprint, item.fact_semantic_fingerprint)
            for item in self.registry_tool_facts
        )
        if len(keys) != len(set(keys)):
            raise ValueError("Tool dispatch facts are not unique")
        if self.registry_tool_facts != self.parent_dispatch_cut.registry.tool_facts:
            raise ValueError("Tool dispatch view does not join parent registry")

    @property
    def planning_input(self) -> FrozenToolCapabilityPlanningInput:
        return self.parent_dispatch_cut.tools


@dataclass(frozen=True, slots=True)
class FrozenSkillCapabilityDispatchView:
    parent_dispatch_cut: FrozenCapabilityDispatchCut = field(repr=False)
    registry_skill_facts: tuple[FrozenSkillCapabilityFact, ...]

    def __post_init__(self) -> None:
        keys = tuple(
            (item.identity.identity_fingerprint, item.fact_semantic_fingerprint)
            for item in self.registry_skill_facts
        )
        if len(keys) != len(set(keys)):
            raise ValueError("Skill dispatch facts are not unique")
        if self.registry_skill_facts != self.parent_dispatch_cut.registry.skill_facts:
            raise ValueError("Skill dispatch view does not join parent registry")

    @property
    def projection_input(self) -> FrozenSkillProjectionInput:
        return self.parent_dispatch_cut.skills


@dataclass(frozen=True, slots=True)
class FrozenToolCapabilityExposureSelection:
    """Pure planner output before selected native wire is materialized."""

    dispatch_view: FrozenToolCapabilityDispatchView = field(repr=False)
    direct_tool_surface: FrozenModelToolSurface
    direct_tool_versions: tuple[ToolCapabilityVersionRef, ...]
    mcp_catalog_route_projection: FrozenMcpRouteProjection
    reusable_direct_projection_set: FrozenNativeToolProjectionSet | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        native = self.dispatch_view.planning_input.native_wire
        _validate_scope(native.conversation_scope_kind, native.scope_subagent_task_id)
        if (
            self.direct_tool_surface.conversation_scope_kind
            is not native.conversation_scope_kind
        ):
            raise ValueError("capability selection Tool surface scope conflicts")
        if tuple(item.name for item in self.direct_tool_surface.tool_specs) != tuple(
            item.provider_name for item in self.direct_tool_versions
        ):
            raise ValueError("capability selection Tool versions drifted")
        reusable = self.reusable_direct_projection_set
        if reusable is not None and (
            reusable.conversation_scope_kind is not native.conversation_scope_kind
            or reusable.scope_subagent_task_id != native.scope_subagent_task_id
            or reusable.native_function_tool_wire_contract_fingerprint
            != native.native_function_tool_wire_contract_fingerprint
            or reusable.tool_versions != self.direct_tool_versions
        ):
            raise ValueError("reusable native projection set does not join selection")

    @property
    def conversation_scope_kind(self) -> ModelInputScopeKind:
        return self.dispatch_view.planning_input.native_wire.conversation_scope_kind

    @property
    def scope_subagent_task_id(self) -> str | None:
        return self.dispatch_view.planning_input.native_wire.scope_subagent_task_id

    @property
    def native_function_tool_wire_contract_fingerprint(self) -> str:
        return self.dispatch_view.planning_input.native_wire.native_function_tool_wire_contract_fingerprint


@dataclass(frozen=True, slots=True)
class FrozenToolCapabilityExposurePlan:
    selection: FrozenToolCapabilityExposureSelection = field(repr=False)
    direct_projection_set: FrozenNativeToolProjectionSet

    def __post_init__(self) -> None:
        if tuple(
            item.name for item in self.selection.direct_tool_surface.tool_specs
        ) != tuple(
            item.provider_name for item in self.direct_projection_set.tool_versions
        ):
            raise ValueError("capability exposure surface/projections drifted")
        if (
            self.selection.direct_tool_surface.conversation_scope_kind
            is not self.direct_projection_set.conversation_scope_kind
        ):
            raise ValueError("capability exposure plan scopes conflict")
        direct_versions = set(self.direct_projection_set.tool_versions)
        if any(
            (
                route.route is ToolCapabilityRouteKind.DIRECT
                and route.version not in direct_versions
            )
            or (
                route.route is ToolCapabilityRouteKind.NEW_MCP_META_ONLY
                and route.version in direct_versions
            )
            for route in self.selection.mcp_catalog_route_projection.routes
        ):
            raise ValueError("MCP route conflicts with the selected native cohort")

    @property
    def dispatch_view(self) -> FrozenToolCapabilityDispatchView:
        return self.selection.dispatch_view

    @property
    def direct_tool_surface(self) -> FrozenModelToolSurface:
        return self.selection.direct_tool_surface

    @property
    def mcp_catalog_route_projection(self) -> FrozenMcpRouteProjection:
        return self.selection.mcp_catalog_route_projection


@dataclass(frozen=True, slots=True)
class PreparedUnavailableDirectMcpGate:
    capability_identity_fingerprint: str
    tool_semantic_fingerprint: str
    provider_tool_name: str
    unavailable_reason_code: str
    supervisor_authority_identity: object = field(repr=False, compare=False)

    @property
    def tool_name(self) -> str:
        return self.provider_tool_name

    @property
    def descriptor_fingerprint(self) -> str:
        return self.tool_semantic_fingerprint

    @property
    def executor_binding_fingerprint(self) -> str:
        return context_fingerprint(
            "unavailable-direct-mcp-gate:v1",
            {
                "identity": self.capability_identity_fingerprint,
                "semantic": self.tool_semantic_fingerprint,
                "provider_name": self.provider_tool_name,
                "reason": self.unavailable_reason_code,
            },
        )


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
    if name.startswith(
        (
            "Capability",
            "Empty",
            "Frozen",
            "Installed",
            "Local",
            "Mcp",
            "Native",
            "Prepared",
            "Tool",
            "MAXIMUM_",
        )
    )
    or name.startswith(
        ("capability_", "canonical_", "freeze_", "native_", "skill_", "tool_")
    )
]
