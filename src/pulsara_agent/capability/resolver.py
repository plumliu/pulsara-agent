"""Resolve one frozen Agent Skills discovery into catalog and active data."""

from __future__ import annotations

from hashlib import sha256
import re

from pulsara_agent.capability.local_skills import (
    LocalSkillDiscovery,
    LocalSkillProvider,
    PreparedLocalSkillRootPolicy,
    SkillDiscoveryDisposition,
)
from pulsara_agent.capability.provider import SkillProjectionOutput
from pulsara_agent.capability.render import (
    SkillProjectionOverbound,
    projection_overbound_diagnostic,
    render_active_skill_prompt,
    render_catalog_prompt,
)
from pulsara_agent.capability.types import (
    ActiveSkillInjection,
    ActiveSkillReason,
    LocalSkillManifest,
    ResolvedSkillCatalogEntry,
    SkillCatalogUnavailableReason,
    SkillDiagnostic,
    SkillDiagnosticSeverity,
    SkillProjectionResolveContext,
)


_EXPLICIT_DOLLAR = re.compile(
    r"(?<![A-Za-z0-9_-])\$([a-z0-9]+(?:-[a-z0-9]+)*)(?![A-Za-z0-9_-])"
)
_EXPLICIT_PREFIX = re.compile(
    r"(?<![A-Za-z0-9_-])skill:([a-z0-9]+(?:-[a-z0-9]+)*)(?![A-Za-z0-9_-])",
    flags=re.IGNORECASE,
)


class LocalSkillCapabilityProvider:
    provider_id = "local-skills"

    def __init__(self, *, provider: LocalSkillProvider | None = None) -> None:
        self.provider = provider or LocalSkillProvider()

    def snapshot_projection_input(
        self,
        *,
        root_policy: PreparedLocalSkillRootPolicy,
        deadline_monotonic: float | None = None,
    ) -> LocalSkillDiscovery:
        return self.provider.discover(
            root_policy, deadline_monotonic=deadline_monotonic
        )

    def resolve_projection_from_snapshot(
        self,
        context: SkillProjectionResolveContext,
        *,
        discovery: LocalSkillDiscovery,
    ) -> SkillProjectionOutput:
        return self._resolve_projection_output(context, discovery=discovery)

    def _resolve_projection_output(
        self,
        context: SkillProjectionResolveContext,
        *,
        discovery: LocalSkillDiscovery,
    ) -> SkillProjectionOutput:
        if discovery.disposition is SkillDiscoveryDisposition.UNAVAILABLE:
            return SkillProjectionOutput(
                diagnostics=discovery.diagnostics,
                catalog_unavailable_reason=discovery.unavailable_reason,
                active_unavailable_reason=discovery.unavailable_reason,
            )
        skills_by_name = {skill.name: skill for skill in discovery.skills}
        catalog_entries = tuple(
            sorted(
                (_catalog_entry(skill) for skill in discovery.skills),
                key=lambda item: item.name,
            )
        )
        active_injections, active_diagnostics, active_unavailable = (
            _active_injections(
                skills_by_name,
                user_input=context.user_input,
                active_skill_names=context.active_skill_names,
            )
        )
        diagnostics = [*discovery.diagnostics, *active_diagnostics]
        catalog_unavailable: SkillCatalogUnavailableReason | None = None
        try:
            catalog = render_catalog_prompt(catalog_entries)
        except SkillProjectionOverbound as exc:
            catalog_unavailable = exc.reason
            diagnostics.append(projection_overbound_diagnostic(exc.reason))
            catalog = None
        active = None
        if active_unavailable is None:
            try:
                active = render_active_skill_prompt(active_injections)
            except SkillProjectionOverbound as exc:
                active_unavailable = exc.reason
                diagnostics.append(projection_overbound_diagnostic(exc.reason))
        return SkillProjectionOutput(
            catalog_entries=catalog_entries,
            active_injections=active_injections if active_unavailable is None else (),
            diagnostics=tuple(diagnostics),
            catalog_prompt=catalog,
            active_skill_prompt=active,
            catalog_unavailable_reason=catalog_unavailable,
            active_unavailable_reason=active_unavailable,
        )


def _catalog_entry(skill: LocalSkillManifest) -> ResolvedSkillCatalogEntry:
    return ResolvedSkillCatalogEntry(
        name=skill.name,
        description=skill.description,
        location=skill.location,
        source=skill.source,
    )


def _active_injections(
    skills_by_name: dict[str, LocalSkillManifest],
    *,
    user_input: str,
    active_skill_names: frozenset[str],
) -> tuple[
    tuple[ActiveSkillInjection, ...],
    tuple[SkillDiagnostic, ...],
    SkillCatalogUnavailableReason | None,
]:
    explicit = _explicit_skill_names(user_input)
    selected = tuple(sorted(set(active_skill_names) | set(explicit)))
    missing = tuple(name for name in selected if name not in skills_by_name)
    if missing:
        return (
            (),
            (
                SkillDiagnostic(
                    severity=SkillDiagnosticSeverity.WARNING,
                    code="active_skill_not_found",
                    message="One or more requested Skills were not found",
                ),
            ),
            SkillCatalogUnavailableReason.ACTIVE_SELECTION_UNAVAILABLE,
        )
    injections: list[ActiveSkillInjection] = []
    for name in selected:
        skill = skills_by_name[name]
        body_digest = "sha256:" + sha256(skill.body.encode("utf-8")).hexdigest()
        injections.append(
            ActiveSkillInjection(
                name=skill.name,
                path=skill.path,
                base_dir=skill.base_dir,
                location=skill.location,
                body=skill.body,
                reason=(
                    ActiveSkillReason.HOST_COMMAND
                    if name in active_skill_names
                    else ActiveSkillReason.EXPLICIT_USER_MENTION
                ),
                source=skill.source,
                manifest_semantic_fingerprint=(
                    skill.manifest_semantic_fingerprint
                ),
                body_digest=body_digest,
                raw_document_digest=skill.raw_document_digest,
            )
        )
    return tuple(injections), (), None


def _explicit_skill_names(user_input: str) -> tuple[str, ...]:
    values = {
        *(_match.group(1) for _match in _EXPLICIT_DOLLAR.finditer(user_input)),
        *(
            _match.group(1).lower()
            for _match in _EXPLICIT_PREFIX.finditer(user_input)
        ),
    }
    return tuple(sorted(values))


__all__ = ["LocalSkillCapabilityProvider"]
