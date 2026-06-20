"""Local V1 skill resolver for one user-message boundary."""

from __future__ import annotations

import re

from pulsara_agent.capability.local_skills import LocalSkillProvider
from pulsara_agent.capability.render import render_active_skill_prompt, render_catalog_prompt
from pulsara_agent.capability.types import (
    ActiveSkillInjection,
    CapabilityDiagnostic,
    CapabilityResolveContext,
    LocalSkillManifest,
    ResolvedCapabilitySet,
    ResolvedSkillCatalogEntry,
)


class LocalSkillResolver:
    def __init__(
        self,
        *,
        provider: LocalSkillProvider | None = None,
        catalog_budget_chars: int = 8000,
    ) -> None:
        self.provider = provider or LocalSkillProvider()
        self.catalog_budget_chars = catalog_budget_chars

    def resolve(self, context: CapabilityResolveContext) -> ResolvedCapabilitySet:
        discovery = self.provider.discover(
            context.workspace_root,
            available_tool_names=context.available_tool_names,
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
        diagnostics = (
            *discovery.diagnostics,
            *active_diagnostics,
            *catalog.diagnostics,
            *active.diagnostics,
        )
        return ResolvedCapabilitySet(
            catalog_entries=catalog_entries,
            active_injections=active_injections,
            visible_tool_names=context.available_tool_names,
            diagnostics=diagnostics,
            catalog_prompt=catalog.text,
            active_skill_prompt=active.text,
        )


def _catalog_entry(skill: LocalSkillManifest) -> ResolvedSkillCatalogEntry:
    return ResolvedSkillCatalogEntry(
        name=skill.name,
        description=skill.description,
        location=skill.location,
        provides_tools=skill.provides_tools,
        when_to_use=skill.when_to_use,
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
