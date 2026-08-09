"""Process-local capability projection for the canonical conversation kernel."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pulsara_agent.capability.resolver import LocalSkillCapabilityProvider
from pulsara_agent.capability.types import CapabilityProjectionResolveContext
from pulsara_agent.memory.scope import MemoryDomainContext
from pulsara_agent.ports.system_prompt import DEFAULT_SYSTEM_PROMPT


@dataclass(frozen=True, slots=True)
class KernelCapabilityProjection:
    system_prompt: str
    catalog_skill_names: tuple[str, ...]
    active_skill_names: tuple[str, ...]
    diagnostic_codes: tuple[str, ...]


class KernelCapabilityComposer:
    def __init__(
        self,
        *,
        workspace_root: Path,
        workspace_kind: str,
        memory_domain: MemoryDomainContext,
        available_tool_names: frozenset[str],
        configured_active_skill_names: frozenset[str] = frozenset(),
        base_system_prompt: str | None = None,
        provider: LocalSkillCapabilityProvider | None = None,
    ) -> None:
        if workspace_kind not in {"project", "transient"}:
            raise ValueError("kernel capability workspace kind is invalid")
        self._workspace_root = workspace_root
        self._workspace_kind = workspace_kind
        self._memory_domain = memory_domain
        self._available_tool_names = available_tool_names
        self._configured = configured_active_skill_names
        self._base = base_system_prompt or DEFAULT_SYSTEM_PROMPT
        self._provider = provider or LocalSkillCapabilityProvider()

    @property
    def configured_active_skill_names(self) -> frozenset[str]:
        return self._configured

    def compose(self, *, user_input: str) -> KernelCapabilityProjection:
        output = self._provider.resolve_projection_for_available_tools(
            CapabilityProjectionResolveContext(
                workspace_root=self._workspace_root,
                workspace_kind=self._workspace_kind,  # type: ignore[arg-type]
                memory_domain=self._memory_domain,
                user_input=user_input,
                active_skill_names=self._configured,
            ),
            available_tool_names=self._available_tool_names,
        )
        parts = [self._base]
        if output.catalog_prompt:
            parts.append(output.catalog_prompt)
        if output.active_skill_prompt:
            parts.append(output.active_skill_prompt)
        return KernelCapabilityProjection(
            system_prompt="\n\n".join(parts),
            catalog_skill_names=tuple(item.name for item in output.catalog_entries),
            active_skill_names=tuple(
                item.name for item in output.active_injections
            ),
            diagnostic_codes=tuple(item.code for item in output.diagnostics),
        )


__all__ = ["KernelCapabilityComposer", "KernelCapabilityProjection"]
