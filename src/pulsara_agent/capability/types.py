"""Typed, immutable Agent Skills projection contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pulsara_agent.capability.contracts import LocalSkillRootKind


class SkillDiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


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


@dataclass(frozen=True, slots=True)
class SkillDiagnostic:
    severity: SkillDiagnosticSeverity
    code: str
    message: str
    path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.severity, SkillDiagnosticSeverity):
            raise TypeError("Skill diagnostic severity is not closed")
        if not self.code or not self.message:
            raise ValueError("Skill diagnostic is incomplete")

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "severity": self.severity.value,
            "code": self.code,
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
