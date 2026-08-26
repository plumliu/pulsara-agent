"""Resolve bundled, loose, and Plugin definitions into one Skill catalog."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import re

from pulsara_agent.capability.bundled_skills import (
    BundledSkillDefinitionsDisposition,
    FrozenBundledSkillDefinitions,
)
from pulsara_agent.capability.local_skills import (
    FrozenLooseSkillDefinitions,
    LOOSE_SKILL_ROOT_ORDER,
    LooseSkillDefinitionsDisposition,
    MAX_ADMITTED_SKILLS,
    PreparedLooseSkillRootPolicy,
    SKILL_DIAGNOSTIC_MESSAGES,
)
from pulsara_agent.capability.plugin_skill_contracts import (
    FrozenPluginSkillDefinitions,
    PluginSkillDefinitionsDisposition,
)
from pulsara_agent.capability.provider import SkillProjectionOutput
from pulsara_agent.capability.render import (
    SkillProjectionOverbound,
    active_projection_overbound_diagnostic,
    catalog_projection_overbound_diagnostic,
    render_active_skill_prompt,
    render_catalog_prompt,
)
from pulsara_agent.capability.types import (
    ActiveSkillInjection,
    ActiveSkillProjectionUnavailableReason,
    ActiveSkillReason,
    BundledSkillOrigin,
    ConflictingSkillCandidateIssue,
    ConflictingSkillCandidateRef,
    InvalidSkillCandidateIssue,
    LooseSkillOrigin,
    PluginSkillOrigin,
    PluginSkillVisibilityScope,
    ProducerUnavailableCause,
    ResolvedSkillCatalogEntry,
    ResolutionUnavailableCause,
    ShadowedSkillCandidateIssue,
    SkillCandidateIssue,
    SkillCatalogUnavailableCause,
    SkillDiagnostic,
    SkillDiagnosticCode,
    SkillDiagnosticSeverity,
    SkillManifest,
    SkillProducerKind,
    SkillProjectionResolveContext,
    SkillResolutionUnavailableReason,
)


_EXPLICIT_DOLLAR = re.compile(
    r"(?<![A-Za-z0-9_-])\$([a-z0-9]+(?:-[a-z0-9]+)*)(?![A-Za-z0-9_-])"
)
_EXPLICIT_PREFIX = re.compile(
    r"(?<![A-Za-z0-9_-])skill:([a-z0-9]+(?:-[a-z0-9]+)*)(?![A-Za-z0-9_-])",
    flags=re.IGNORECASE,
)


class EffectiveSkillCatalogDisposition(StrEnum):
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class CompleteEffectiveSkillCatalogInspection:
    root_policy: PreparedLooseSkillRootPolicy = field(repr=False)
    winners: tuple[SkillManifest, ...]
    candidate_issues: tuple[SkillCandidateIssue, ...]
    disposition: EffectiveSkillCatalogDisposition = field(
        default=EffectiveSkillCatalogDisposition.COMPLETE, init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.root_policy, PreparedLooseSkillRootPolicy):
            raise TypeError("effective Skill inspection lacks a root policy")
        names = tuple(item.name for item in self.winners)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("effective Skill winners are not sorted and unique")
        if len(self.winners) > MAX_ADMITTED_SKILLS:
            raise ValueError("effective Skill inspection exceeds winner bound")
        winners = {(item.name, item.origin, item.path) for item in self.winners}
        for issue in self.candidate_issues:
            if isinstance(issue, InvalidSkillCandidateIssue) and isinstance(
                issue.origin, BundledSkillOrigin
            ):
                raise ValueError("effective Skill inspection has a bundled invalid issue")
            if isinstance(issue, ShadowedSkillCandidateIssue) and (
                issue.name,
                issue.winner_origin,
                issue.winner_path,
            ) not in winners:
                raise ValueError("shadowed Skill issue does not join a winner")
        issue_keys = tuple(
            skill_candidate_issue_sort_key(item) for item in self.candidate_issues
        )
        if issue_keys != tuple(sorted(issue_keys)) or len(issue_keys) != len(
            set(issue_keys)
        ):
            raise ValueError("effective Skill issues are not deterministic and unique")

@dataclass(frozen=True, slots=True)
class UnavailableEffectiveSkillCatalogInspection:
    root_policy: PreparedLooseSkillRootPolicy = field(repr=False)
    unavailable_causes: tuple[SkillCatalogUnavailableCause, ...]
    disposition: EffectiveSkillCatalogDisposition = field(
        default=EffectiveSkillCatalogDisposition.UNAVAILABLE, init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.root_policy, PreparedLooseSkillRootPolicy):
            raise TypeError("effective Skill inspection lacks a root policy")
        if not self.unavailable_causes:
            raise ValueError("unavailable effective Skill inspection has no cause")
        producer_causes = tuple(
            item for item in self.unavailable_causes if isinstance(item, ProducerUnavailableCause)
        )
        resolution_causes = tuple(
            item for item in self.unavailable_causes if isinstance(item, ResolutionUnavailableCause)
        )
        if producer_causes and resolution_causes:
            raise ValueError("producer and resolution causes cannot coexist")
        if len(resolution_causes) > 1:
            raise ValueError("effective Skill inspection has multiple resolution causes")
        producer_order = tuple(item.producer_kind for item in producer_causes)
        expected_order = tuple(
            item
            for item in (
                SkillProducerKind.LOOSE,
                SkillProducerKind.PLUGIN,
                SkillProducerKind.BUNDLED,
            )
            if item in producer_order
        )
        if producer_order != expected_order or len(producer_order) != len(
            set(producer_order)
        ):
            raise ValueError("producer unavailable causes are not ordered and unique")

    @property
    def winners(self) -> tuple[SkillManifest, ...]:
        return ()

    @property
    def candidate_issues(self) -> tuple[SkillCandidateIssue, ...]:
        return ()

EffectiveSkillCatalogInspection = (
    CompleteEffectiveSkillCatalogInspection
    | UnavailableEffectiveSkillCatalogInspection
)


class SkillCatalogResolver:
    """The single seven-tier precedence and final catalog-bound owner."""

    def resolve(
        self,
        loose: FrozenLooseSkillDefinitions,
        plugin: FrozenPluginSkillDefinitions,
        bundled: FrozenBundledSkillDefinitions,
    ) -> EffectiveSkillCatalogInspection:
        causes: list[ProducerUnavailableCause] = []
        if loose.disposition is LooseSkillDefinitionsDisposition.UNAVAILABLE:
            assert loose.unavailable_cause is not None
            causes.append(loose.unavailable_cause)
        if plugin.disposition is PluginSkillDefinitionsDisposition.UNAVAILABLE:
            assert plugin.unavailable_cause is not None
            causes.append(plugin.unavailable_cause)
        if bundled.disposition is BundledSkillDefinitionsDisposition.UNAVAILABLE:
            assert bundled.unavailable_cause is not None
            causes.append(bundled.unavailable_cause)
        if causes:
            return UnavailableEffectiveSkillCatalogInspection(
                root_policy=loose.root_policy,
                unavailable_causes=tuple(causes),
            )

        grouped: dict[str, list[SkillManifest]] = {}
        for candidate in (
            *loose.candidates,
            *plugin.candidates,
            *bundled.candidates,
        ):
            grouped.setdefault(candidate.name, []).append(candidate)
        winners: list[SkillManifest] = []
        issues: list[SkillCandidateIssue] = [
            *loose.invalid_issues,
            *plugin.invalid_issues,
        ]
        for name in sorted(grouped):
            by_tier: dict[int, list[SkillManifest]] = {}
            for candidate in grouped[name]:
                by_tier.setdefault(_skill_tier(candidate), []).append(candidate)
            winner: SkillManifest | None = None
            for tier in sorted(by_tier):
                candidates = sorted(
                    by_tier[tier], key=skill_candidate_precedence_key
                )
                if winner is not None:
                    for candidate in candidates:
                        issues.append(
                            ShadowedSkillCandidateIssue(
                                path=candidate.path,
                                origin=candidate.origin,
                                name=name,
                                winner_origin=winner.origin,
                                winner_path=winner.path,
                                diagnostic_codes=candidate.diagnostic_codes,
                            )
                        )
                    continue
                first_origin = candidates[0].origin
                if isinstance(first_origin, PluginSkillOrigin) and len(candidates) > 1:
                    issues.append(
                        ConflictingSkillCandidateIssue(
                            name,
                            first_origin.visibility_scope,
                            tuple(
                                ConflictingSkillCandidateRef(
                                    candidate.path, candidate.origin
                                )
                                for candidate in candidates
                                if isinstance(candidate.origin, PluginSkillOrigin)
                            ),
                        )
                    )
                    continue
                if len(candidates) != 1:
                    raise RuntimeError("non-Plugin Skill tier is not unique")
                winner = candidates[0]
                winners.append(winner)
        input_valid = {
            (item.name, item.origin, item.path)
            for item in (*loose.candidates, *plugin.candidates, *bundled.candidates)
        }
        allocated_valid = {
            (item.name, item.origin, item.path) for item in winners
        } | {
            (item.name, item.origin, item.path)
            for item in issues
            if isinstance(item, ShadowedSkillCandidateIssue)
        } | {
            (item.name, candidate.origin, candidate.path)
            for item in issues
            if isinstance(item, ConflictingSkillCandidateIssue)
            for candidate in item.candidates
        }
        if allocated_valid != input_valid:
            raise RuntimeError("central Skill resolution did not allocate every candidate")
        if len(winners) > MAX_ADMITTED_SKILLS:
            return UnavailableEffectiveSkillCatalogInspection(
                root_policy=loose.root_policy,
                unavailable_causes=(
                    ResolutionUnavailableCause(
                        SkillResolutionUnavailableReason.EFFECTIVE_WINNER_BOUND_EXCEEDED,
                        SkillDiagnostic(
                            SkillDiagnosticSeverity.WARNING,
                            SkillDiagnosticCode.WINNER_BOUND_EXCEEDED,
                            SKILL_DIAGNOSTIC_MESSAGES[
                                SkillDiagnosticCode.WINNER_BOUND_EXCEEDED
                            ],
                        ),
                    ),
                ),
            )
        ordered_winners = tuple(sorted(winners, key=lambda item: item.name))
        entries = tuple(_catalog_entry(item) for item in ordered_winners)
        try:
            render_catalog_prompt(entries)
        except SkillProjectionOverbound as exc:
            if exc.reason is not SkillResolutionUnavailableReason.CATALOG_PROJECTION_OVERBOUND:
                raise RuntimeError("catalog renderer emitted an active outcome") from exc
            return UnavailableEffectiveSkillCatalogInspection(
                root_policy=loose.root_policy,
                unavailable_causes=(
                    ResolutionUnavailableCause(
                        SkillResolutionUnavailableReason.CATALOG_PROJECTION_OVERBOUND,
                        catalog_projection_overbound_diagnostic(),
                    ),
                ),
            )
        return CompleteEffectiveSkillCatalogInspection(
            root_policy=loose.root_policy,
            winners=ordered_winners,
            candidate_issues=tuple(sorted(issues, key=skill_candidate_issue_sort_key)),
        )


class SkillCatalogCapabilityProvider:
    provider_id = "skill-catalog"

    def resolve_projection_from_snapshot(
        self,
        context: SkillProjectionResolveContext,
        *,
        inspection: EffectiveSkillCatalogInspection,
    ) -> SkillProjectionOutput:
        diagnostics = list(runtime_skill_diagnostics(inspection))
        if isinstance(inspection, UnavailableEffectiveSkillCatalogInspection):
            return SkillProjectionOutput(
                diagnostics=tuple(diagnostics),
                catalog_unavailable_causes=inspection.unavailable_causes,
                active_unavailable_reason=(
                    ActiveSkillProjectionUnavailableReason.ACTIVE_SELECTION_UNAVAILABLE
                ),
            )
        skills_by_name = {skill.name: skill for skill in inspection.winners}
        catalog_entries = tuple(_catalog_entry(item) for item in inspection.winners)
        active, active_diagnostics, active_unavailable = _active_injections(
            skills_by_name,
            user_input=context.user_input,
            active_skill_names=context.active_skill_names,
        )
        diagnostics.extend(active_diagnostics)
        catalog_prompt = render_catalog_prompt(catalog_entries)
        active_prompt = None
        if active_unavailable is None:
            try:
                active_prompt = render_active_skill_prompt(active)
            except SkillProjectionOverbound as exc:
                if not isinstance(exc.reason, ActiveSkillProjectionUnavailableReason):
                    raise RuntimeError("active renderer emitted a catalog outcome") from exc
                active_unavailable = exc.reason
                diagnostics.append(active_projection_overbound_diagnostic())
        return SkillProjectionOutput(
            catalog_entries=catalog_entries,
            active_injections=active if active_unavailable is None else (),
            diagnostics=tuple(diagnostics),
            catalog_prompt=catalog_prompt,
            active_skill_prompt=active_prompt,
            active_unavailable_reason=active_unavailable,
        )


def inspection_diagnostics(
    inspection: EffectiveSkillCatalogInspection,
) -> tuple[SkillDiagnostic, ...]:
    """Full path-specific inspection projection used by doctor."""

    if isinstance(inspection, UnavailableEffectiveSkillCatalogInspection):
        result: list[SkillDiagnostic] = []
        for cause in inspection.unavailable_causes:
            if isinstance(cause, ProducerUnavailableCause):
                result.extend(cause.diagnostics)
            else:
                result.append(cause.diagnostic)
        return tuple(result)
    result = []
    for issue in inspection.candidate_issues:
        if isinstance(issue, InvalidSkillCandidateIssue):
            result.extend(issue.diagnostics)
        elif isinstance(issue, ShadowedSkillCandidateIssue):
            result.extend(
                SkillDiagnostic(
                    _canonical_diagnostic_severity(code),
                    code,
                    SKILL_DIAGNOSTIC_MESSAGES[code],
                    issue.path,
                )
                for code in issue.diagnostic_codes
            )
        elif isinstance(issue, ConflictingSkillCandidateIssue):
            result.append(
                SkillDiagnostic(
                    _canonical_diagnostic_severity(issue.diagnostic_code),
                    issue.diagnostic_code,
                    SKILL_DIAGNOSTIC_MESSAGES[issue.diagnostic_code],
                )
            )
        else:  # pragma: no cover - closed issue union
            raise TypeError("Skill candidate issue union is open")
    for winner in inspection.winners:
        result.extend(
            SkillDiagnostic(
                _canonical_diagnostic_severity(code),
                code,
                SKILL_DIAGNOSTIC_MESSAGES[code],
                winner.path,
            )
            for code in winner.diagnostic_codes
        )
    return tuple(result)


def runtime_skill_diagnostics(
    inspection: EffectiveSkillCatalogInspection,
) -> tuple[SkillDiagnostic, ...]:
    """Bounded semantic-set projection; it is not the inspection truth."""

    codes = {item.code for item in inspection_diagnostics(inspection)}
    return tuple(
        SkillDiagnostic(
            _canonical_diagnostic_severity(code),
            code,
            SKILL_DIAGNOSTIC_MESSAGES[code],
        )
        for code in sorted(codes, key=lambda item: item.value)
    )


def skill_candidate_precedence_key(item: SkillManifest) -> tuple[int, str]:
    return _skill_tier(item), item.path.as_posix()


def skill_candidate_issue_sort_key(
    item: SkillCandidateIssue,
) -> tuple[str, int, str, str]:
    if isinstance(item, ConflictingSkillCandidateIssue):
        first = item.candidates[0]
        tier = (
            len(LOOSE_SKILL_ROOT_ORDER)
            if item.tier is PluginSkillVisibilityScope.WORKSPACE
            else len(LOOSE_SKILL_ROOT_ORDER) + 1
        )
        return item.name, tier, first.path.as_posix(), item.kind.value
    name = (
        (item.declared_name or "")
        if isinstance(item, InvalidSkillCandidateIssue)
        else item.name
    )
    origin = item.origin
    tier = _origin_tier(origin)
    return name, tier, item.path.as_posix(), item.kind.value


def _skill_tier(item: SkillManifest) -> int:
    return _origin_tier(item.origin)


def _origin_tier(origin) -> int:
    if isinstance(origin, LooseSkillOrigin):
        return LOOSE_SKILL_ROOT_ORDER.index(origin.root_kind)
    if isinstance(origin, PluginSkillOrigin):
        return (
            len(LOOSE_SKILL_ROOT_ORDER)
            if origin.visibility_scope is PluginSkillVisibilityScope.WORKSPACE
            else len(LOOSE_SKILL_ROOT_ORDER) + 1
        )
    if isinstance(origin, BundledSkillOrigin):
        return len(LOOSE_SKILL_ROOT_ORDER) + 2
    raise TypeError("Skill origin union is open")


def _catalog_entry(skill: SkillManifest) -> ResolvedSkillCatalogEntry:
    return ResolvedSkillCatalogEntry(
        name=skill.name,
        description=skill.description,
        location=skill.location,
        origin=skill.origin,
    )


def _active_injections(
    skills_by_name: dict[str, SkillManifest],
    *,
    user_input: str,
    active_skill_names: frozenset[str],
) -> tuple[
    tuple[ActiveSkillInjection, ...],
    tuple[SkillDiagnostic, ...],
    ActiveSkillProjectionUnavailableReason | None,
]:
    explicit = _explicit_skill_names(user_input)
    selected = tuple(sorted(set(active_skill_names) | set(explicit)))
    if any(name not in skills_by_name for name in selected):
        return (
            (),
            (
                SkillDiagnostic(
                    SkillDiagnosticSeverity.WARNING,
                    SkillDiagnosticCode.ACTIVE_SKILL_NOT_FOUND,
                    SKILL_DIAGNOSTIC_MESSAGES[SkillDiagnosticCode.ACTIVE_SKILL_NOT_FOUND],
                ),
            ),
            ActiveSkillProjectionUnavailableReason.ACTIVE_SELECTION_UNAVAILABLE,
        )
    injections: list[ActiveSkillInjection] = []
    for name in selected:
        skill = skills_by_name[name]
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
                origin=skill.origin,
                manifest_semantic_fingerprint=skill.manifest_semantic_fingerprint,
                body_digest="sha256:" + sha256(skill.body.encode("utf-8")).hexdigest(),
                raw_document_digest=skill.raw_document_digest,
            )
        )
    return tuple(injections), (), None


def _explicit_skill_names(user_input: str) -> tuple[str, ...]:
    values = {
        *(_match.group(1) for _match in _EXPLICIT_DOLLAR.finditer(user_input)),
        *(_match.group(1).lower() for _match in _EXPLICIT_PREFIX.finditer(user_input)),
    }
    return tuple(sorted(values))


def _canonical_diagnostic_severity(code: SkillDiagnosticCode) -> SkillDiagnosticSeverity:
    if code in {
        SkillDiagnosticCode.HOST_EXTENSION_IGNORED,
        SkillDiagnosticCode.UNKNOWN_EXTENSION_IGNORED,
        SkillDiagnosticCode.BODY_OVER_500_LINES,
        SkillDiagnosticCode.BODY_ESTIMATE_OVER_5000_TOKENS,
    }:
        return SkillDiagnosticSeverity.INFO
    if code in {
        SkillDiagnosticCode.ROOT_ESCAPE,
        SkillDiagnosticCode.ROOT_NOT_DIRECTORY,
        SkillDiagnosticCode.DIRECTORY_ESCAPE,
        SkillDiagnosticCode.FILE_ESCAPE,
        SkillDiagnosticCode.ENUMERATION_RACED,
        SkillDiagnosticCode.READ_RACED,
        SkillDiagnosticCode.USER_HOME_CONFIGURATION_INVALID,
        SkillDiagnosticCode.LOOSE_ROOT_ALIAS,
        SkillDiagnosticCode.BUNDLED_DEFINITIONS_UNAVAILABLE,
        SkillDiagnosticCode.BUNDLED_INVENTORY_MISMATCH,
        SkillDiagnosticCode.PLUGIN_DEFINITIONS_UNAVAILABLE,
        SkillDiagnosticCode.INVALID_UTF8,
    }:
        return SkillDiagnosticSeverity.ERROR
    return SkillDiagnosticSeverity.WARNING


__all__ = [
    "CompleteEffectiveSkillCatalogInspection",
    "EffectiveSkillCatalogDisposition",
    "EffectiveSkillCatalogInspection",
    "SkillCatalogCapabilityProvider",
    "SkillCatalogResolver",
    "UnavailableEffectiveSkillCatalogInspection",
    "inspection_diagnostics",
    "runtime_skill_diagnostics",
    "skill_candidate_issue_sort_key",
    "skill_candidate_precedence_key",
]
