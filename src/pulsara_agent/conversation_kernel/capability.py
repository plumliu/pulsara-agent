"""Process-local capability projection for the canonical conversation kernel."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from pulsara_agent.capability.provider import CapabilityProjectionOutput
from pulsara_agent.capability.local_skills import LocalSkillDiscovery
from pulsara_agent.capability.resolver import LocalSkillCapabilityProvider
from pulsara_agent.capability.types import CapabilityProjectionResolveContext
from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.primitives.context import context_fingerprint


@dataclass(frozen=True, slots=True)
class FrozenKernelCapabilityProjectionInput:
    discovery: LocalSkillDiscovery = field(repr=False)
    available_tool_names: frozenset[str]
    snapshot_fingerprint: str

    def __post_init__(self) -> None:
        expected = _capability_projection_input_fingerprint(
            self.discovery, self.available_tool_names
        )
        if self.snapshot_fingerprint != expected:
            raise ValueError("capability projection input fingerprint mismatch")


class KernelCapabilityComposer:
    def __init__(
        self,
        *,
        workspace_root: Path,
        workspace_kind: str,
        memory_domain: MemoryDomainContext,
        available_tool_names: frozenset[str],
        configured_active_skill_names: frozenset[str] = frozenset(),
        provider: LocalSkillCapabilityProvider | None = None,
    ) -> None:
        if workspace_kind not in {"project", "transient"}:
            raise ValueError("kernel capability workspace kind is invalid")
        self._workspace_root = workspace_root
        self._workspace_kind = workspace_kind
        self._memory_domain = memory_domain
        self._available_tool_names = available_tool_names
        self._configured = configured_active_skill_names
        self._provider = provider or LocalSkillCapabilityProvider()

    @property
    def configured_active_skill_names(self) -> frozenset[str]:
        return self._configured

    def resolve_projection(
        self,
        *,
        user_input: str,
        available_tool_names: frozenset[str],
    ) -> CapabilityProjectionOutput:
        return self._provider.resolve_projection_for_available_tools(
            CapabilityProjectionResolveContext(
                workspace_root=self._workspace_root,
                workspace_kind=self._workspace_kind,  # type: ignore[arg-type]
                memory_domain=self._memory_domain,
                user_input=user_input,
                active_skill_names=self._configured,
            ),
            available_tool_names=(available_tool_names & self._available_tool_names),
        )

    def freeze_projection_input(
        self, *, available_tool_names: frozenset[str]
    ) -> FrozenKernelCapabilityProjectionInput:
        allowed = available_tool_names & self._available_tool_names
        discovery = self._provider.snapshot_projection_input(
            workspace_root=self._workspace_root,
            available_tool_names=allowed,
        )
        return FrozenKernelCapabilityProjectionInput(
            discovery=discovery,
            available_tool_names=allowed,
            snapshot_fingerprint=_capability_projection_input_fingerprint(
                discovery, allowed
            ),
        )

    def resolve_projection_from_frozen(
        self,
        frozen: FrozenKernelCapabilityProjectionInput,
        *,
        user_input: str,
    ) -> CapabilityProjectionOutput:
        if not frozen.available_tool_names <= self._available_tool_names:
            raise ValueError("frozen capability surface is foreign")
        return self._provider.resolve_projection_from_snapshot(
            CapabilityProjectionResolveContext(
                workspace_root=self._workspace_root,
                workspace_kind=self._workspace_kind,  # type: ignore[arg-type]
                memory_domain=self._memory_domain,
                user_input=user_input,
                active_skill_names=self._configured,
            ),
            available_tool_names=frozen.available_tool_names,
            discovery=frozen.discovery,
        )


def _capability_projection_input_fingerprint(
    discovery: LocalSkillDiscovery,
    available_tool_names: frozenset[str],
) -> str:
    return context_fingerprint(
        "pulsara:frozen-kernel-capability-projection-input:v1",
        {
            "tools": tuple(sorted(available_tool_names)),
            "skills": tuple(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "path": str(skill.path),
                    "base_dir": str(skill.base_dir),
                    "location": skill.location,
                    "content_digest": "sha256:"
                    + sha256(skill.content.encode("utf-8")).hexdigest(),
                    "source": skill.source,
                    "when_to_use": skill.when_to_use,
                    "provides_tools": skill.provides_tools,
                    "suggested_tools": skill.suggested_tools,
                    "required_binaries": skill.required_binaries,
                    "optional_binaries": skill.optional_binaries,
                    "external_services": skill.external_services,
                    "network_required": skill.network_required,
                    "auth_required": skill.auth_required,
                    "cli_usage_kind": skill.cli_usage_kind,
                    "disable_model_invocation": skill.disable_model_invocation,
                    "user_invocable": skill.user_invocable,
                    "body_too_large": skill.body_too_large,
                }
                for skill in discovery.skills
            ),
            "diagnostics": tuple(
                (
                    diagnostic.severity,
                    diagnostic.code,
                    diagnostic.message,
                    None if diagnostic.path is None else str(diagnostic.path),
                )
                for diagnostic in discovery.diagnostics
            ),
        },
    )


__all__ = ["FrozenKernelCapabilityProjectionInput", "KernelCapabilityComposer"]
