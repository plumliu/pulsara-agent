"""Typed runtime contract for local skill capability resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pulsara_agent.message import Msg

if TYPE_CHECKING:
    from pulsara_agent.memory.scope import MemoryDomainContext


WorkspaceKind = Literal["project", "transient"]
CapabilityDiagnosticSeverity = Literal["info", "warning", "error"]
SkillSource = Literal["workspace", "user", "bundled"]
ActiveSkillReason = Literal["explicit_user_mention", "host_command"]
SkillAuthRequired = Literal["none", "optional", "required"]
SkillCliUsageKind = Literal["none", "read", "write", "mixed"]


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
class CapabilityExecutionSurfaceSnapshotContext:
    """Pre-RunStart context: static declarations and frozen bindings only."""

    workspace_root: Path
    workspace_kind: WorkspaceKind
    available_tool_names: frozenset[str]
    mcp_installation_id: str


@dataclass(frozen=True, slots=True)
class CapabilityProjectionResolveContext:
    """Post-RunStart context used only for model-visible projections."""

    workspace_root: Path
    workspace_kind: WorkspaceKind
    memory_domain: "MemoryDomainContext | None"
    user_input: str
    prior_messages: tuple[Msg, ...] = ()
    active_skill_names: frozenset[str] = frozenset()
    plan_active: bool = False


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
    suggested_tools: tuple[str, ...] = ()
    required_binaries: tuple[str, ...] = ()
    optional_binaries: tuple[str, ...] = ()
    external_services: tuple[str, ...] = ()
    network_required: bool = False
    auth_required: SkillAuthRequired = "none"
    cli_usage_kind: SkillCliUsageKind = "none"
    disable_model_invocation: bool = False
    user_invocable: bool = True
    body_too_large: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedSkillCatalogEntry:
    name: str
    description: str
    location: str
    provides_tools: tuple[str, ...] = ()
    suggested_tools: tuple[str, ...] = ()
    required_binaries: tuple[str, ...] = ()
    optional_binaries: tuple[str, ...] = ()
    external_services: tuple[str, ...] = ()
    network_required: bool = False
    auth_required: SkillAuthRequired = "none"
    cli_usage_kind: SkillCliUsageKind = "none"
    when_to_use: str | None = None
    source: SkillSource = "workspace"


@dataclass(frozen=True, slots=True)
class ActiveSkillInjection:
    name: str
    path: Path
    base_dir: Path
    location: str
    content: str
    reason: ActiveSkillReason
    suggested_tools: tuple[str, ...] = ()
    required_binaries: tuple[str, ...] = ()
    optional_binaries: tuple[str, ...] = ()
    external_services: tuple[str, ...] = ()
    network_required: bool = False
    auth_required: SkillAuthRequired = "none"
    cli_usage_kind: SkillCliUsageKind = "none"
    source: SkillSource = "workspace"


@dataclass(frozen=True, slots=True)
class RenderedCapabilityPrompt:
    text: str | None
    diagnostics: tuple[CapabilityDiagnostic, ...] = ()
    fragments: tuple["RenderedCapabilityPromptFragment", ...] = ()
    source_entry_count: int = 0


@dataclass(frozen=True, slots=True)
class RenderedCapabilityPromptFragment:
    container_id: str
    fragment_role: Literal["prefix", "entry", "suffix", "static"]
    static_scope: Literal["container_wrapper", "projection_wrapper"] | None
    source_stable_name: str | None
    text: str
