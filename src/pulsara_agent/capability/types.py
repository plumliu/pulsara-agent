"""Typed, immutable Agent Skills definition and projection contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
import re
from typing import TypeAlias

from pulsara_agent.capability.contracts import LocalSkillRootKind


class SkillDiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class SkillDiagnosticCode(StrEnum):
    MISSING_DOCUMENT = "skill_missing_document"
    DOCUMENT_OVERBOUND = "skill_document_overbound"
    INVALID_UTF8 = "skill_invalid_utf8"
    MISSING_FRONTMATTER = "skill_missing_frontmatter"
    FRONTMATTER_OVERBOUND = "skill_frontmatter_overbound"
    INVALID_FRONTMATTER_YAML = "skill_invalid_frontmatter_yaml"
    HOST_EXTENSION_IGNORED = "skill_host_extension_ignored"
    UNKNOWN_EXTENSION_IGNORED = "skill_unknown_extension_ignored"
    INVALID_NAME = "skill_invalid_name"
    DIRECTORY_NAME_MISMATCH = "skill_directory_name_mismatch"
    INVALID_DESCRIPTION = "skill_invalid_description"
    INVALID_LICENSE = "skill_invalid_license"
    INVALID_COMPATIBILITY = "skill_invalid_compatibility"
    INVALID_METADATA = "skill_invalid_metadata"
    LOCATION_OVERBOUND = "skill_location_overbound"
    BODY_OVER_500_LINES = "skill_body_over_500_lines"
    BODY_ESTIMATE_OVER_5000_TOKENS = "skill_body_estimate_over_5000_tokens"
    ROOT_ESCAPE = "skill_root_escape"
    ROOT_NOT_DIRECTORY = "skill_root_not_directory"
    DIRECT_CHILD_BOUND_EXCEEDED = "skill_direct_child_bound_exceeded"
    DIRECTORY_ESCAPE = "skill_directory_escape"
    FILE_ESCAPE = "skill_file_escape"
    ENUMERATION_RACED = "skill_enumeration_raced"
    READ_RACED = "skill_read_raced"
    DISCOVERY_BYTE_BOUND_EXCEEDED = "skill_discovery_byte_bound_exceeded"
    WINNER_BOUND_EXCEEDED = "skill_winner_bound_exceeded"
    CATALOG_PROJECTION_BOUND_EXCEEDED = "skill_catalog_projection_bound_exceeded"
    ACTIVE_SKILL_NOT_FOUND = "active_skill_not_found"
    PROJECTION_OVERBOUND = "skill_projection_overbound"
    USER_HOME_CONFIGURATION_INVALID = "skill_user_home_configuration_invalid"
    LOOSE_ROOT_ALIAS = "skill_loose_root_alias"
    BUNDLED_DEFINITIONS_UNAVAILABLE = "skill_bundled_definitions_unavailable"
    BUNDLED_INVENTORY_MISMATCH = "skill_bundled_inventory_mismatch"
    PLUGIN_DEFINITIONS_UNAVAILABLE = "skill_plugin_definitions_unavailable"
    PLUGIN_SAME_TIER_NAME_CONFLICT = "skill_plugin_same_tier_name_conflict"


if len(SkillDiagnosticCode) != 35:
    raise RuntimeError("Skill diagnostic vocabulary must contain exactly 35 codes")


class SkillSource(StrEnum):
    WORKSPACE = "workspace"
    USER = "user"
    PLUGIN = "plugin"
    BUNDLED = "bundled"


class ActiveSkillReason(StrEnum):
    EXPLICIT_USER_MENTION = "explicit_user_mention"
    HOST_COMMAND = "host_command"


class SkillAuthoringDiagnosticCode(StrEnum):
    BODY_OVER_500_LINES = "skill_body_over_500_lines"
    BODY_ESTIMATE_OVER_5000_TOKENS = "skill_body_estimate_over_5000_tokens"


class SkillProducerKind(StrEnum):
    LOOSE = "LOOSE"
    PLUGIN = "PLUGIN"
    BUNDLED = "BUNDLED"


class SkillProducerUnavailableReason(StrEnum):
    LOOSE_CONFIGURATION_INVALID = "LOOSE_CONFIGURATION_INVALID"
    LOOSE_DISCOVERY_RACED = "LOOSE_DISCOVERY_RACED"
    LOOSE_DISCOVERY_OVERBOUND = "LOOSE_DISCOVERY_OVERBOUND"
    PLUGIN_VIEW_UNAVAILABLE = "PLUGIN_VIEW_UNAVAILABLE"
    PLUGIN_RESOURCE_UNAVAILABLE = "PLUGIN_RESOURCE_UNAVAILABLE"
    PLUGIN_DISCOVERY_RACED = "PLUGIN_DISCOVERY_RACED"
    BUNDLED_RESOURCE_UNAVAILABLE = "BUNDLED_RESOURCE_UNAVAILABLE"
    BUNDLED_INVENTORY_MISMATCH = "BUNDLED_INVENTORY_MISMATCH"
    BUNDLED_DEFINITION_INVALID = "BUNDLED_DEFINITION_INVALID"
    BUNDLED_DISCOVERY_RACED = "BUNDLED_DISCOVERY_RACED"


class SkillResolutionUnavailableReason(StrEnum):
    EFFECTIVE_WINNER_BOUND_EXCEEDED = "EFFECTIVE_WINNER_BOUND_EXCEEDED"
    CATALOG_PROJECTION_OVERBOUND = "CATALOG_PROJECTION_OVERBOUND"


class ActiveSkillProjectionUnavailableReason(StrEnum):
    ACTIVE_SELECTION_UNAVAILABLE = "ACTIVE_SELECTION_UNAVAILABLE"
    ACTIVE_PROJECTION_OVERBOUND = "ACTIVE_PROJECTION_OVERBOUND"


@dataclass(frozen=True, slots=True)
class SkillDiagnostic:
    severity: SkillDiagnosticSeverity
    code: SkillDiagnosticCode
    message: str
    path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.severity, SkillDiagnosticSeverity):
            raise TypeError("Skill diagnostic severity is not closed")
        if not isinstance(self.code, SkillDiagnosticCode):
            raise TypeError("Skill diagnostic code is not closed")
        if not self.message:
            raise ValueError("Skill diagnostic is incomplete")

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "severity": self.severity.value,
            "code": self.code.value,
            "message": self.message,
        }
        if self.path is not None:
            data["path"] = str(self.path)
        return data


@dataclass(frozen=True, slots=True)
class LooseSkillOrigin:
    root_kind: LocalSkillRootKind

    def __post_init__(self) -> None:
        if not isinstance(self.root_kind, LocalSkillRootKind):
            raise TypeError("loose Skill root kind is not closed")


@dataclass(frozen=True, slots=True)
class BundledSkillOrigin:
    package_relative_skill_directory: str

    def __post_init__(self) -> None:
        value = self.package_relative_skill_directory
        parts = PurePosixPath(value).parts
        if (
            len(parts) != 2
            or parts[0] != "bundled_skills"
            or any(item in {"", ".", ".."} for item in parts)
            or value.startswith("/")
            or value.endswith("/")
            or "\\" in value
        ):
            raise ValueError("bundled Skill origin is not canonical")

    @property
    def skill_name(self) -> str:
        return PurePosixPath(self.package_relative_skill_directory).name


class PluginSkillVisibilityScope(StrEnum):
    USER = "USER"
    WORKSPACE = "WORKSPACE"


@dataclass(frozen=True, slots=True)
class PluginSkillOrigin:
    visibility_scope: PluginSkillVisibilityScope
    plugin_id: str
    package_install_id: str
    package_relative_skill_directory: str
    workspace_state_key: str | None = None

    def __post_init__(self) -> None:
        relative = PurePosixPath(self.package_relative_skill_directory)
        if (
            not isinstance(self.visibility_scope, PluginSkillVisibilityScope)
            or not re.fullmatch(
                r"(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?",
                self.plugin_id,
            )
            or len(self.plugin_id.encode("utf-8")) > 64
            or not re.fullmatch(r"pkg_[0-9a-f]{32}", self.package_install_id)
            or len(relative.parts) != 2
            or relative.parts[0] != "skills"
            or any(part in {"", ".", ".."} for part in relative.parts)
            or self.package_relative_skill_directory.startswith("/")
            or "\\" in self.package_relative_skill_directory
        ):
            raise ValueError("Plugin Skill origin is not canonical")
        if (self.visibility_scope is PluginSkillVisibilityScope.WORKSPACE) != (
            self.workspace_state_key is not None
        ):
            raise ValueError("Plugin Skill visibility conflicts with workspace identity")


SkillDefinitionOrigin: TypeAlias = (
    LooseSkillOrigin | PluginSkillOrigin | BundledSkillOrigin
)


def skill_source_for_origin(origin: SkillDefinitionOrigin) -> SkillSource:
    if isinstance(origin, BundledSkillOrigin):
        return SkillSource.BUNDLED
    if isinstance(origin, PluginSkillOrigin):
        return SkillSource.PLUGIN
    if not isinstance(origin, LooseSkillOrigin):
        raise TypeError("Skill origin union is open")
    if origin.root_kind in {
        LocalSkillRootKind.WORKSPACE_PULSARA,
        LocalSkillRootKind.WORKSPACE_AGENTS,
    }:
        return SkillSource.WORKSPACE
    return SkillSource.USER


def skill_origin_label(origin: SkillDefinitionOrigin) -> str:
    if isinstance(origin, BundledSkillOrigin):
        return "bundled:pulsara-agent"
    if isinstance(origin, PluginSkillOrigin):
        prefix = (
            "workspace-plugin"
            if origin.visibility_scope is PluginSkillVisibilityScope.WORKSPACE
            else "user-plugin"
        )
        return f"{prefix}:{origin.plugin_id}@{origin.package_install_id}"
    if not isinstance(origin, LooseSkillOrigin):
        raise TypeError("Skill origin union is open")
    return {
        LocalSkillRootKind.WORKSPACE_PULSARA: "workspace:.pulsara/skills",
        LocalSkillRootKind.WORKSPACE_AGENTS: "workspace:.agents/skills",
        LocalSkillRootKind.USER_PULSARA: "user:${PULSARA_HOME}/skills",
        LocalSkillRootKind.USER_AGENTS: "user:~/.agents/skills",
    }[origin.root_kind]


@dataclass(frozen=True, slots=True)
class SkillProjectionResolveContext:
    """Trigger-specific input resolved against one frozen effective catalog."""

    user_input: str
    active_skill_names: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class SkillManifest:
    """One validated, source-neutral Agent Skills definition candidate."""

    name: str
    description: str
    license: str | None
    compatibility: str | None
    metadata: tuple[tuple[str, str], ...]
    path: Path
    base_dir: Path
    location: str
    body: str
    raw_document_digest: str
    manifest_semantic_fingerprint: str
    origin: SkillDefinitionOrigin
    diagnostic_codes: tuple[SkillDiagnosticCode, ...] = ()
    authoring_diagnostic_codes: tuple[SkillAuthoringDiagnosticCode, ...] = ()
    raw_document: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(
            self.origin, (LooseSkillOrigin, PluginSkillOrigin, BundledSkillOrigin)
        ):
            raise TypeError("Skill manifest origin is not closed")
        if self.path.name != "SKILL.md" or self.base_dir != self.path.parent:
            raise ValueError("Skill physical identity is inconsistent")
        if isinstance(self.origin, BundledSkillOrigin) and (
            self.origin.skill_name != self.name
        ):
            raise ValueError("bundled Skill origin/name join conflicts")
        if not self.name or not self.description or not self.location:
            raise ValueError("Skill manifest identity is incomplete")
        if self.metadata != tuple(sorted(self.metadata)) or len(
            {key for key, _value in self.metadata}
        ) != len(self.metadata):
            raise ValueError("Skill metadata is not sorted and unique")
        if not self.raw_document_digest.startswith("sha256:"):
            raise ValueError("Skill raw document digest is invalid")
        if not self.manifest_semantic_fingerprint.startswith("sha256:"):
            raise ValueError("Skill semantic fingerprint is invalid")
        if len(set(self.diagnostic_codes)) != len(self.diagnostic_codes) or any(
            not isinstance(item, SkillDiagnosticCode)
            for item in self.diagnostic_codes
        ):
            raise ValueError("Skill candidate diagnostics are invalid")
        if len(set(self.authoring_diagnostic_codes)) != len(
            self.authoring_diagnostic_codes
        ) or any(
            not isinstance(item, SkillAuthoringDiagnosticCode)
            for item in self.authoring_diagnostic_codes
        ):
            raise ValueError("Skill authoring diagnostics are invalid")

    @property
    def source(self) -> SkillSource:
        return skill_source_for_origin(self.origin)

    @property
    def origin_label(self) -> str:
        return skill_origin_label(self.origin)


class SkillCandidateIssueKind(StrEnum):
    INVALID = "INVALID"
    SHADOWED = "SHADOWED"
    CONFLICTING = "CONFLICTING"


@dataclass(frozen=True, slots=True)
class InvalidSkillCandidateIssue:
    path: Path
    origin: SkillDefinitionOrigin
    diagnostics: tuple[SkillDiagnostic, ...]
    declared_name: str | None = None
    kind: SkillCandidateIssueKind = field(
        default=SkillCandidateIssueKind.INVALID, init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(
            self.origin, (LooseSkillOrigin, PluginSkillOrigin, BundledSkillOrigin)
        ):
            raise TypeError("invalid candidate origin is not closed")
        if not self.diagnostics or any(
            item.path != self.path for item in self.diagnostics
        ):
            raise ValueError("invalid candidate diagnostics are incomplete")


@dataclass(frozen=True, slots=True)
class ShadowedSkillCandidateIssue:
    path: Path
    origin: SkillDefinitionOrigin
    name: str
    winner_origin: SkillDefinitionOrigin
    winner_path: Path
    diagnostic_codes: tuple[SkillDiagnosticCode, ...] = ()
    kind: SkillCandidateIssueKind = field(
        default=SkillCandidateIssueKind.SHADOWED, init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(
            self.origin, (LooseSkillOrigin, PluginSkillOrigin, BundledSkillOrigin)
        ) or not (
            isinstance(
                self.winner_origin,
                (LooseSkillOrigin, PluginSkillOrigin, BundledSkillOrigin),
            )
        ):
            raise TypeError("shadowed candidate origins are not closed")
        if not self.name or self.path == self.winner_path:
            raise ValueError("shadowed candidate identity is incomplete")
        if len(set(self.diagnostic_codes)) != len(self.diagnostic_codes) or any(
            not isinstance(item, SkillDiagnosticCode)
            for item in self.diagnostic_codes
        ):
            raise ValueError("shadowed candidate diagnostics are invalid")


@dataclass(frozen=True, slots=True)
class ConflictingSkillCandidateRef:
    path: Path
    origin: PluginSkillOrigin


@dataclass(frozen=True, slots=True)
class ConflictingSkillCandidateIssue:
    name: str
    tier: PluginSkillVisibilityScope
    candidates: tuple[ConflictingSkillCandidateRef, ...]
    diagnostic_code: SkillDiagnosticCode = field(
        default=SkillDiagnosticCode.PLUGIN_SAME_TIER_NAME_CONFLICT, init=False
    )
    kind: SkillCandidateIssueKind = field(
        default=SkillCandidateIssueKind.CONFLICTING, init=False
    )

    def __post_init__(self) -> None:
        if not self.name or len(self.candidates) < 2:
            raise ValueError("Plugin Skill conflict group is incomplete")
        if any(
            item.origin.visibility_scope is not self.tier
            for item in self.candidates
        ):
            raise ValueError("Plugin Skill conflict group crosses tiers")
        keys = tuple(
            (
                item.origin.plugin_id,
                item.origin.package_install_id,
                item.path.as_posix(),
            )
            for item in self.candidates
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("Plugin Skill conflict candidates are not deterministic")


SkillCandidateIssue: TypeAlias = (
    InvalidSkillCandidateIssue
    | ShadowedSkillCandidateIssue
    | ConflictingSkillCandidateIssue
)


@dataclass(frozen=True, slots=True)
class ProducerUnavailableCause:
    producer_kind: SkillProducerKind
    reason: SkillProducerUnavailableReason
    diagnostics: tuple[SkillDiagnostic, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.producer_kind, SkillProducerKind) or not isinstance(
            self.reason, SkillProducerUnavailableReason
        ):
            raise TypeError("Skill producer unavailable cause is not closed")
        if not self.diagnostics:
            raise ValueError("Skill producer unavailable cause has no diagnostic")
        expected_prefix = f"{self.producer_kind.value}_"
        if not self.reason.value.startswith(expected_prefix):
            raise ValueError("Skill producer unavailable reason conflicts")


@dataclass(frozen=True, slots=True)
class ResolutionUnavailableCause:
    reason: SkillResolutionUnavailableReason
    diagnostic: SkillDiagnostic

    def __post_init__(self) -> None:
        if not isinstance(self.reason, SkillResolutionUnavailableReason):
            raise TypeError("Skill resolution unavailable reason is not closed")


SkillCatalogUnavailableCause: TypeAlias = (
    ProducerUnavailableCause | ResolutionUnavailableCause
)


@dataclass(frozen=True, slots=True)
class ResolvedSkillCatalogEntry:
    name: str
    description: str
    location: str
    origin: SkillDefinitionOrigin

    def __post_init__(self) -> None:
        if not isinstance(
            self.origin, (LooseSkillOrigin, PluginSkillOrigin, BundledSkillOrigin)
        ):
            raise TypeError("Skill catalog origin is not closed")
        if not self.name or not self.description or not self.location:
            raise ValueError("Skill catalog entry is incomplete")

    @property
    def source(self) -> SkillSource:
        return skill_source_for_origin(self.origin)

    @property
    def origin_label(self) -> str:
        return skill_origin_label(self.origin)


@dataclass(frozen=True, slots=True)
class ActiveSkillInjection:
    name: str
    path: Path
    base_dir: Path
    location: str
    body: str
    reason: ActiveSkillReason
    origin: SkillDefinitionOrigin
    manifest_semantic_fingerprint: str
    body_digest: str
    raw_document_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, ActiveSkillReason):
            raise TypeError("active Skill reason is not closed")
        if not isinstance(
            self.origin, (LooseSkillOrigin, PluginSkillOrigin, BundledSkillOrigin)
        ):
            raise TypeError("active Skill origin is not closed")
        if self.path.name != "SKILL.md" or self.base_dir != self.path.parent:
            raise ValueError("active Skill physical identity is inconsistent")
        for value in (
            self.manifest_semantic_fingerprint,
            self.body_digest,
            self.raw_document_digest,
        ):
            if not value.startswith("sha256:"):
                raise ValueError("active Skill fingerprint is invalid")

    @property
    def source(self) -> SkillSource:
        return skill_source_for_origin(self.origin)


__all__ = [
    "ActiveSkillInjection",
    "ActiveSkillProjectionUnavailableReason",
    "ActiveSkillReason",
    "BundledSkillOrigin",
    "ConflictingSkillCandidateIssue",
    "ConflictingSkillCandidateRef",
    "InvalidSkillCandidateIssue",
    "LooseSkillOrigin",
    "PluginSkillOrigin",
    "PluginSkillVisibilityScope",
    "ProducerUnavailableCause",
    "ResolvedSkillCatalogEntry",
    "ResolutionUnavailableCause",
    "ShadowedSkillCandidateIssue",
    "SkillAuthoringDiagnosticCode",
    "SkillCandidateIssue",
    "SkillCandidateIssueKind",
    "SkillCatalogUnavailableCause",
    "SkillDefinitionOrigin",
    "SkillDiagnostic",
    "SkillDiagnosticCode",
    "SkillDiagnosticSeverity",
    "SkillManifest",
    "SkillProducerKind",
    "SkillProducerUnavailableReason",
    "SkillProjectionResolveContext",
    "SkillResolutionUnavailableReason",
    "SkillSource",
    "skill_origin_label",
    "skill_source_for_origin",
]
