"""Typed, immutable Agent Skills projection contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

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
    DISCOVERY_DEADLINE_EXPIRED = "skill_discovery_deadline_expired"
    DUPLICATE_NAME = "skill_duplicate_name"
    WINNER_BOUND_EXCEEDED = "skill_winner_bound_exceeded"
    CATALOG_PROJECTION_BOUND_EXCEEDED = "skill_catalog_projection_bound_exceeded"
    ACTIVE_SKILL_NOT_FOUND = "active_skill_not_found"
    PROJECTION_OVERBOUND = "skill_projection_overbound"
    USER_HOME_CONFIGURATION_INVALID = "skill_user_home_configuration_invalid"


class SkillSource(StrEnum):
    WORKSPACE = "workspace"
    USER = "user"


class ActiveSkillReason(StrEnum):
    EXPLICIT_USER_MENTION = "explicit_user_mention"
    HOST_COMMAND = "host_command"


class SkillAuthoringDiagnosticCode(StrEnum):
    BODY_OVER_500_LINES = "skill_body_over_500_lines"
    BODY_ESTIMATE_OVER_5000_TOKENS = "skill_body_estimate_over_5000_tokens"


class SkillCatalogUnavailableReason(StrEnum):
    DISCOVERY_RACED = "DISCOVERY_RACED"
    DISCOVERY_OVERBOUND = "DISCOVERY_OVERBOUND"
    CATALOG_OVERBOUND = "CATALOG_OVERBOUND"
    PROVIDER_BUDGET_UNAVAILABLE = "PROVIDER_BUDGET_UNAVAILABLE"
    ACTIVE_SELECTION_UNAVAILABLE = "ACTIVE_SELECTION_UNAVAILABLE"
    USER_HOME_CONFIGURATION_INVALID = "USER_HOME_CONFIGURATION_INVALID"


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
class SkillProjectionResolveContext:
    """Trigger-specific input resolved against one frozen Skill discovery."""

    user_input: str
    active_skill_names: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class LocalSkillManifest:
    """One validated Agent Skills SKILL.md document."""

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
    root_kind: LocalSkillRootKind
    authoring_diagnostic_codes: tuple[SkillAuthoringDiagnosticCode, ...] = ()
    raw_document: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.root_kind, LocalSkillRootKind):
            raise TypeError("Skill root kind is not closed")
        if self.path.name != "SKILL.md" or self.base_dir != self.path.parent:
            raise ValueError("Skill physical identity is inconsistent")
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
        if len(set(self.authoring_diagnostic_codes)) != len(
            self.authoring_diagnostic_codes
        ) or any(
            not isinstance(item, SkillAuthoringDiagnosticCode)
            for item in self.authoring_diagnostic_codes
        ):
            raise ValueError("Skill authoring diagnostics are invalid")

    @property
    def source(self) -> SkillSource:
        if self.root_kind in {
            LocalSkillRootKind.WORKSPACE_PULSARA,
            LocalSkillRootKind.WORKSPACE_AGENTS,
        }:
            return SkillSource.WORKSPACE
        return SkillSource.USER


@dataclass(frozen=True, slots=True)
class ResolvedSkillCatalogEntry:
    name: str
    description: str
    location: str
    source: SkillSource

    def __post_init__(self) -> None:
        if not isinstance(self.source, SkillSource):
            raise TypeError("Skill catalog source is not closed")
        if not self.name or not self.description or not self.location:
            raise ValueError("Skill catalog entry is incomplete")


@dataclass(frozen=True, slots=True)
class ActiveSkillInjection:
    name: str
    path: Path
    base_dir: Path
    location: str
    body: str
    reason: ActiveSkillReason
    source: SkillSource
    manifest_semantic_fingerprint: str
    body_digest: str
    raw_document_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, ActiveSkillReason):
            raise TypeError("active Skill reason is not closed")
        if not isinstance(self.source, SkillSource):
            raise TypeError("active Skill source is not closed")
        if self.path.name != "SKILL.md" or self.base_dir != self.path.parent:
            raise ValueError("active Skill physical identity is inconsistent")
        for value in (
            self.manifest_semantic_fingerprint,
            self.body_digest,
            self.raw_document_digest,
        ):
            if not value.startswith("sha256:"):
                raise ValueError("active Skill fingerprint is invalid")
