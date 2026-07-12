"""Local skill capability provider for one user-message boundary."""

from __future__ import annotations

import re

from pulsara_agent.capability.local_skills import LocalSkillProvider
from pulsara_agent.capability.provider import (
    CapabilityProjectionOutput,
)
from pulsara_agent.capability.render import (
    DEFAULT_CATALOG_BUDGET_CHARS,
    render_active_skill_prompt,
    render_catalog_prompt,
)
from pulsara_agent.capability.skill_health import SkillHealthResolver
from pulsara_agent.capability.types import (
    ActiveSkillInjection,
    CapabilityDiagnostic,
    CapabilityProjectionResolveContext,
    LocalSkillManifest,
    ResolvedSkillCatalogEntry,
)
from pulsara_agent.primitives.capability import CapabilityExecutionSurfaceIdentityFact


class LocalSkillCapabilityProvider:
    provider_id = "local-skills"

    def __init__(
        self,
        *,
        provider: LocalSkillProvider | None = None,
        skill_health_resolver: SkillHealthResolver | None = None,
        catalog_budget_chars: int = DEFAULT_CATALOG_BUDGET_CHARS,
    ) -> None:
        self.provider = provider or LocalSkillProvider()
        self.skill_health_resolver = skill_health_resolver or SkillHealthResolver()
        self.catalog_budget_chars = catalog_budget_chars

    def _resolve_projection_output(
        self,
        context: CapabilityProjectionResolveContext,
        *,
        available_tool_names: frozenset[str],
    ) -> CapabilityProjectionOutput:
        discovery = self.provider.discover(
            context.workspace_root,
            available_tool_names=available_tool_names,
        )
        skills_by_name = {skill.name: skill for skill in discovery.skills}
        catalog_entries = tuple(
            _catalog_entry(skill) for skill in discovery.skills if not skill.disable_model_invocation
        )
        active_injections, active_diagnostics = _active_injections(
            skills_by_name,
            user_input=context.user_input,
            active_skill_names=context.active_skill_names,
        )
        catalog = render_catalog_prompt(catalog_entries, budget_chars=self.catalog_budget_chars)
        active = render_active_skill_prompt(active_injections)
        health_diagnostics = self.skill_health_resolver.diagnostics_for_active_skills(active_injections)
        diagnostics = (
            *discovery.diagnostics,
            *active_diagnostics,
            *health_diagnostics,
            *catalog.diagnostics,
            *active.diagnostics,
        )
        return CapabilityProjectionOutput(
            catalog_entries=catalog_entries,
            active_injections=active_injections,
            diagnostics=diagnostics,
            catalog_prompt=catalog.text,
            active_skill_prompt=active.text,
            catalog_rendered=catalog,
            active_skill_rendered=active,
        )

    def resolve_projection(
        self,
        context: CapabilityProjectionResolveContext,
        *,
        execution_surface: CapabilityExecutionSurfaceIdentityFact,
    ) -> CapabilityProjectionOutput:
        return self._resolve_projection_output(
            context,
            available_tool_names=frozenset(
                entry.capability_name for entry in execution_surface.entries
            ),
        )


def _catalog_entry(skill: LocalSkillManifest) -> ResolvedSkillCatalogEntry:
    return ResolvedSkillCatalogEntry(
        name=skill.name,
        description=skill.description,
        location=skill.location,
        provides_tools=skill.provides_tools,
        suggested_tools=skill.suggested_tools,
        required_binaries=skill.required_binaries,
        optional_binaries=skill.optional_binaries,
        external_services=skill.external_services,
        network_required=skill.network_required,
        auth_required=skill.auth_required,
        cli_usage_kind=skill.cli_usage_kind,
        when_to_use=skill.when_to_use,
        source=skill.source,
    )


def _active_injections(
    skills_by_name: dict[str, LocalSkillManifest],
    *,
    user_input: str,
    active_skill_names: frozenset[str],
) -> tuple[tuple[ActiveSkillInjection, ...], tuple[CapabilityDiagnostic, ...]]:
    diagnostics: list[CapabilityDiagnostic] = []
    active_names: list[str] = []
    for name in sorted(skills_by_name):
        if name in active_skill_names or _explicitly_mentions_skill(user_input, name):
            active_names.append(name)
    for name in sorted(active_skill_names - set(skills_by_name)):
        diagnostics.append(
            CapabilityDiagnostic(
                severity="warning",
                code="skill_activation_not_found",
                message=f"Requested skill was not found: {name}",
            )
        )
    injections: list[ActiveSkillInjection] = []
    for name in active_names:
        skill = skills_by_name[name]
        if skill.body_too_large:
            continue
        injections.append(
            ActiveSkillInjection(
                name=skill.name,
                path=skill.path,
                base_dir=skill.base_dir,
                location=skill.location,
                content=skill.content,
                reason="host_command" if name in active_skill_names else "explicit_user_mention",
                suggested_tools=skill.suggested_tools,
                required_binaries=skill.required_binaries,
                optional_binaries=skill.optional_binaries,
                external_services=skill.external_services,
                network_required=skill.network_required,
                auth_required=skill.auth_required,
                cli_usage_kind=skill.cli_usage_kind,
                source=skill.source,
            )
        )
    return tuple(injections), tuple(diagnostics)


def _explicitly_mentions_skill(user_input: str, skill_name: str) -> bool:
    escaped = re.escape(skill_name)
    token_boundary = r"(?![A-Za-z0-9_-])"
    prefix_boundary = r"(?<![A-Za-z0-9_-])"
    return bool(
        re.search(prefix_boundary + r"\$" + escaped + token_boundary, user_input)
        or re.search(prefix_boundary + r"skill:" + escaped + token_boundary, user_input, flags=re.IGNORECASE)
    )
