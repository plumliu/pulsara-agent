"""Plugin Skill producer contracts with no dependency on Plugin runtime code."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pulsara_agent.capability.types import (
    InvalidSkillCandidateIssue,
    PluginSkillOrigin,
    ProducerUnavailableCause,
    SkillManifest,
    SkillProducerKind,
)


class PluginSkillDefinitionsDisposition(StrEnum):
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class FrozenPluginSkillDefinitions:
    disposition: PluginSkillDefinitionsDisposition
    candidates: tuple[SkillManifest, ...] = ()
    invalid_issues: tuple[InvalidSkillCandidateIssue, ...] = ()
    unavailable_cause: ProducerUnavailableCause | None = None

    def __post_init__(self) -> None:
        complete = self.disposition is PluginSkillDefinitionsDisposition.COMPLETE
        if complete != (self.unavailable_cause is None):
            raise ValueError("Plugin Skill definitions disposition conflicts")
        if not complete and (self.candidates or self.invalid_issues):
            raise ValueError("unavailable Plugin Skill definitions contain facts")
        if self.unavailable_cause is not None and (
            self.unavailable_cause.producer_kind is not SkillProducerKind.PLUGIN
        ):
            raise ValueError("Plugin Skill definitions contain a foreign cause")
        candidate_keys = tuple(_candidate_key(item) for item in self.candidates)
        if candidate_keys != tuple(sorted(candidate_keys)) or len(
            candidate_keys
        ) != len(set(candidate_keys)):
            raise ValueError("Plugin Skill candidates are not deterministic")
        issue_keys = tuple(_issue_key(item) for item in self.invalid_issues)
        if issue_keys != tuple(sorted(issue_keys)) or len(issue_keys) != len(
            set(issue_keys)
        ):
            raise ValueError("Plugin Skill invalid issues are not deterministic")
        if any(
            not isinstance(item.origin, PluginSkillOrigin)
            for item in (*self.candidates, *self.invalid_issues)
        ):
            raise ValueError("Plugin Skill batch contains a foreign origin")


def _candidate_key(item: SkillManifest) -> tuple[str, str, str, str, str]:
    origin = item.origin
    if not isinstance(origin, PluginSkillOrigin):
        raise TypeError("Plugin Skill candidate has a foreign origin")
    return (
        origin.visibility_scope.value,
        origin.plugin_id,
        origin.package_install_id,
        item.name,
        item.path.as_posix(),
    )


def _issue_key(
    item: InvalidSkillCandidateIssue,
) -> tuple[str, str, str, str, str]:
    origin = item.origin
    if not isinstance(origin, PluginSkillOrigin):
        raise TypeError("Plugin Skill issue has a foreign origin")
    return (
        origin.visibility_scope.value,
        origin.plugin_id,
        origin.package_install_id,
        item.declared_name or "",
        item.path.as_posix(),
    )


__all__ = [
    "FrozenPluginSkillDefinitions",
    "PluginSkillDefinitionsDisposition",
]
