"""Typed runtime contract for local skill capability resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from pulsara_agent.message import Msg

if TYPE_CHECKING:
    from pulsara_agent.memory.scope import MemoryDomainContext


WorkspaceKind = Literal["project", "transient"]
CapabilityDiagnosticSeverity = Literal["info", "warning", "error"]
SkillSource = Literal["workspace"]
ActiveSkillReason = Literal["explicit_user_mention", "host_command"]


@dataclass(frozen=True, slots=True)
class CapabilityDiagnostic:
    severity: CapabilityDiagnosticSeverity
    code: str
    message: str
    path: Path | None = None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.path is not None:
            data["path"] = str(self.path)
        return data


@dataclass(frozen=True, slots=True)
class CapabilityResolveContext:
    workspace_root: Path
    workspace_kind: WorkspaceKind
    memory_domain: "MemoryDomainContext | None"
    available_tool_names: frozenset[str]
    user_input: str
    prior_messages: tuple[Msg, ...] = ()
    active_skill_names: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class LocalSkillManifest:
    name: str
    description: str
    path: Path
    base_dir: Path
    location: str
    content: str
    source: SkillSource = "workspace"
    when_to_use: str | None = None
    provides_tools: tuple[str, ...] = ()
    disable_model_invocation: bool = False
    user_invocable: bool = True
    body_too_large: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedSkillCatalogEntry:
    name: str
    description: str
    location: str
    provides_tools: tuple[str, ...] = ()
    when_to_use: str | None = None


@dataclass(frozen=True, slots=True)
class ActiveSkillInjection:
    name: str
    path: Path
    base_dir: Path
    location: str
    content: str
    reason: ActiveSkillReason


@dataclass(frozen=True, slots=True)
class RenderedCapabilityPrompt:
    text: str | None
    diagnostics: tuple[CapabilityDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedCapabilitySet:
    catalog_entries: tuple[ResolvedSkillCatalogEntry, ...] = ()
    active_injections: tuple[ActiveSkillInjection, ...] = ()
    visible_tool_names: frozenset[str] = field(default_factory=frozenset)
    diagnostics: tuple[CapabilityDiagnostic, ...] = ()
    catalog_prompt: str | None = None
    active_skill_prompt: str | None = None


class CapabilityResolver(Protocol):
    def resolve(self, context: CapabilityResolveContext) -> ResolvedCapabilitySet:
        """Resolve model-visible capability inputs for one user message."""


class NoopCapabilityResolver:
    def resolve(self, context: CapabilityResolveContext) -> ResolvedCapabilitySet:
        return ResolvedCapabilitySet(visible_tool_names=context.available_tool_names)
