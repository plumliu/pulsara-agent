"""Third explicit Skill definition producer over one captured Plugin view."""

from __future__ import annotations

from collections import defaultdict

from pulsara_agent.capability.local_skills import enrich_skill_document
from pulsara_agent.capability.plugin_skill_contracts import (
    FrozenPluginSkillDefinitions,
    PluginSkillDefinitionsDisposition,
)
from pulsara_agent.capability.types import (
    InvalidSkillCandidateIssue,
    PluginSkillOrigin,
    PluginSkillVisibilityScope,
    ProducerUnavailableCause,
    SkillDiagnostic,
    SkillDiagnosticCode,
    SkillDiagnosticSeverity,
    SkillProducerKind,
    SkillProducerUnavailableReason,
)
from pulsara_agent.plugins.contracts import (
    EnabledPluginViewDisposition,
    PluginComponentObservationDisposition,
    PluginScopeKind,
)
from pulsara_agent.plugins.view import FrozenEnabledPluginView


class PluginSkillDefinitionProducer:
    """Adapt exact parsed Plugin Skill facts without rescanning package roots."""

    def observe(
        self, view: FrozenEnabledPluginView
    ) -> FrozenPluginSkillDefinitions:
        if view.disposition is EnabledPluginViewDisposition.UNAVAILABLE:
            return _unavailable(
                SkillProducerUnavailableReason.PLUGIN_VIEW_UNAVAILABLE
            )
        candidates = []
        issues: list[InvalidSkillCandidateIssue] = []
        for instance in view.instances:
            component = instance.skills
            if (
                component.disposition
                is PluginComponentObservationDisposition.UNAVAILABLE
            ):
                return _unavailable(
                    SkillProducerUnavailableReason.PLUGIN_RESOURCE_UNAVAILABLE
                )
            visibility = (
                PluginSkillVisibilityScope.USER
                if instance.identity.scope is PluginScopeKind.USER
                else PluginSkillVisibilityScope.WORKSPACE
            )
            for candidate in component.candidates:
                origin = PluginSkillOrigin(
                    visibility,
                    instance.identity.plugin_id,
                    instance.state.current_package_install_id,
                    candidate.relative_directory.as_posix(),
                    instance.identity.workspace_state_key,
                )
                candidates.append(
                    enrich_skill_document(
                        candidate.parsed,
                        path=candidate.document_path,
                        location=str(candidate.document_path.parent),
                        origin=origin,
                        diagnostic_codes=tuple(
                            item.code for item in candidate.diagnostics
                        ),
                    )
                )
            grouped: dict[object, list[SkillDiagnostic]] = defaultdict(list)
            for diagnostic in component.invalid_diagnostics:
                grouped[diagnostic.path].append(diagnostic)
            for raw_path, diagnostics in grouped.items():
                if raw_path is None:
                    continue
                path = raw_path
                relative = f"skills/{path.parent.name}"
                origin = PluginSkillOrigin(
                    visibility,
                    instance.identity.plugin_id,
                    instance.state.current_package_install_id,
                    relative,
                    instance.identity.workspace_state_key,
                )
                issues.append(
                    InvalidSkillCandidateIssue(
                        path,
                        origin,
                        tuple(
                            sorted(diagnostics, key=lambda item: item.code.value)
                        ),
                    )
                )
        return FrozenPluginSkillDefinitions(
            PluginSkillDefinitionsDisposition.COMPLETE,
            tuple(sorted(candidates, key=_candidate_key)),
            tuple(sorted(issues, key=_issue_key)),
        )


def _candidate_key(item) -> tuple[str, str, str, str, str]:
    origin = item.origin
    if not isinstance(origin, PluginSkillOrigin):
        raise TypeError("Plugin Skill producer created a foreign origin")
    return (
        origin.visibility_scope.value,
        origin.plugin_id,
        origin.package_install_id,
        item.name,
        item.path.as_posix(),
    )


def _issue_key(item) -> tuple[str, str, str, str, str]:
    origin = item.origin
    if not isinstance(origin, PluginSkillOrigin):
        raise TypeError("Plugin Skill producer created a foreign issue")
    return (
        origin.visibility_scope.value,
        origin.plugin_id,
        origin.package_install_id,
        item.declared_name or "",
        item.path.as_posix(),
    )


def _unavailable(
    reason: SkillProducerUnavailableReason,
) -> FrozenPluginSkillDefinitions:
    return FrozenPluginSkillDefinitions(
        PluginSkillDefinitionsDisposition.UNAVAILABLE,
        unavailable_cause=ProducerUnavailableCause(
            SkillProducerKind.PLUGIN,
            reason,
            (
                SkillDiagnostic(
                    SkillDiagnosticSeverity.ERROR,
                    SkillDiagnosticCode.PLUGIN_DEFINITIONS_UNAVAILABLE,
                    "Plugin Skill definitions are unavailable",
                ),
            ),
        ),
    )


__all__ = ["PluginSkillDefinitionProducer"]
