"""Process-local capability projection for the canonical conversation kernel."""

from __future__ import annotations

from pathlib import Path

from pulsara_agent.capability.provider import CapabilityProjectionOutput
from pulsara_agent.capability.resolver import LocalSkillCapabilityProvider
from pulsara_agent.capability.types import CapabilityProjectionResolveContext
from pulsara_agent.memory.scope import MemoryDomainContext


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


__all__ = ["KernelCapabilityComposer"]
