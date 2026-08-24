"""Agent Skills parser and sole four-root local catalog scanner."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
from time import monotonic
from typing import Any, TypeAlias

import yaml
from yaml.events import (
    AliasEvent,
    DocumentStartEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)

from pulsara_agent.capability.contracts import LocalSkillRootKind
from pulsara_agent.capability.local_skill_source_binding import (
    open_absolute_directory_nofollow,
)
from pulsara_agent.capability.pulsara_home import (
    PulsaraHomeDisposition,
    PulsaraHomeResolution,
    PulsaraHomeUnavailableReason,
    resolve_pulsara_home,
)
from pulsara_agent.capability.render import (
    SkillProjectionOverbound,
    render_catalog_prompt,
)
from pulsara_agent.capability.types import (
    LocalSkillManifest,
    ResolvedSkillCatalogEntry,
    SkillAuthoringDiagnosticCode,
    SkillCatalogUnavailableReason,
    SkillDiagnostic,
    SkillDiagnosticCode,
    SkillDiagnosticSeverity,
)
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint


WORKSPACE_PRODUCT_SKILL_ROOT_PARTS = (".pulsara", "skills")
WORKSPACE_AGENTS_SKILL_ROOT_PARTS = (".agents", "skills")
USER_PRODUCT_SKILL_ROOT_PARTS = (".pulsara", "skills")
USER_AGENTS_SKILL_ROOT_PARTS = (".agents", "skills")
USER_PRODUCT_LOCATION_PREFIX = "${PULSARA_HOME}/skills"
SKILL_FILE_NAME = "SKILL.md"
BUNDLED_SKILL_PROVENANCE_FILE_NAME = ".pulsara-skill-source.json"

AGENT_SKILLS_CONTRACT_ID = "pulsara.agent-skills-core.v1"
MAX_SKILL_FILE_BYTES = 64 * 1024
MAX_SKILL_FRONTMATTER_BYTES = 32 * 1024
MAX_SKILL_YAML_NODES = 512
MAX_SKILL_YAML_DEPTH = 16
MAX_SKILL_NAME_BYTES = 64
MAX_SKILL_DESCRIPTION_CHARS = 1024
MAX_SKILL_LICENSE_BYTES = 1024
MAX_SKILL_COMPATIBILITY_CHARS = 500
MAX_SKILL_METADATA_ITEMS = 64
MAX_SKILL_METADATA_KEY_BYTES = 128
MAX_SKILL_METADATA_VALUE_BYTES = 1024
MAX_SKILL_METADATA_BYTES = 16 * 1024
MAX_SKILL_LOCATION_BYTES = 1024
MAX_SKILL_DIRECT_CHILDREN = 1024
MAX_ADMITTED_SKILLS = 64
MAX_DISCOVERY_SKILL_BYTES = 16 * 1024 * 1024

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_STANDARD_FIELDS = frozenset(
    {"name", "description", "license", "compatibility", "metadata"}
)
_HOST_EXTENSION_FIELDS = frozenset(
    {
        "when_to_use",
        "when-to-use",
        "disable-model-invocation",
        "disable_model_invocation",
        "user-invocable",
        "user_invocable",
        "argument-hint",
        "argument_hint",
        "arguments",
        "allowed-tools",
        "allowed_tools",
        "model",
        "effort",
        "context",
        "agent",
        "hooks",
        "shell",
        "paths",
        "provides_tools",
        "suggested_tools",
        "required_binaries",
        "optional_binaries",
        "external_services",
        "network_required",
        "auth_required",
        "cli_usage_kind",
        "allowed_scopes",
        "blocked_scopes",
    }
)
_ROOT_ORDER = (
    LocalSkillRootKind.WORKSPACE_PULSARA,
    LocalSkillRootKind.WORKSPACE_AGENTS,
    LocalSkillRootKind.USER_PULSARA,
    LocalSkillRootKind.USER_AGENTS,
)
_ROOT_LOCATION_PREFIX = {
    LocalSkillRootKind.WORKSPACE_PULSARA: ".pulsara/skills",
    LocalSkillRootKind.WORKSPACE_AGENTS: ".agents/skills",
    LocalSkillRootKind.USER_PULSARA: USER_PRODUCT_LOCATION_PREFIX,
    LocalSkillRootKind.USER_AGENTS: "~/.agents/skills",
}
_ROOT_POLICY_CONSTRUCTOR = object()
_DISCOVERY_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_DISCOVERY_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class SkillDiscoveryDisposition(StrEnum):
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


class LocalSkillCandidateIssueKind(StrEnum):
    INVALID = "INVALID"
    SHADOWED = "SHADOWED"


@dataclass(frozen=True, slots=True)
class ParsedLocalSkillDocument:
    """Root-neutral result of the one production SKILL.md parser."""

    name: str
    description: str
    license: str | None
    compatibility: str | None
    metadata: tuple[tuple[str, str], ...]
    body: str
    raw_document_digest: str
    manifest_semantic_fingerprint: str
    authoring_diagnostic_codes: tuple[SkillAuthoringDiagnosticCode, ...] = ()
    raw_document: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.name or not self.description:
            raise ValueError("parsed Skill document is incomplete")
        if self.metadata != tuple(sorted(self.metadata)):
            raise ValueError("parsed Skill metadata is not deterministic")
        if not self.raw_document_digest.startswith("sha256:"):
            raise ValueError("parsed Skill raw digest is invalid")
        if not self.manifest_semantic_fingerprint.startswith("sha256:"):
            raise ValueError("parsed Skill semantic fingerprint is invalid")


@dataclass(frozen=True, slots=True)
class SkillDocumentParseResult:
    parsed: ParsedLocalSkillDocument | None
    declared_name: str | None
    diagnostics: tuple[SkillDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.parsed is None and not self.diagnostics:
            raise ValueError("invalid Skill parse result has no diagnostic")
        if self.parsed is not None and self.declared_name != self.parsed.name:
            raise ValueError("parsed Skill declared name conflicts")
        if any(item.path is not None for item in self.diagnostics):
            raise ValueError("root-neutral parser emitted a physical path")


@dataclass(frozen=True, slots=True, init=False)
class PreparedSkillRootBinding:
    root_kind: LocalSkillRootKind
    path: Path
    containment_root: Path
    location_prefix: str
    precedence_ordinal: int
    _owner_authority: object = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        root_kind: LocalSkillRootKind,
        path: Path,
        containment_root: Path,
        location_prefix: str,
        precedence_ordinal: int,
        _owner_authority: object,
        _constructor: object,
    ) -> None:
        if _constructor is not _ROOT_POLICY_CONSTRUCTOR:
            raise TypeError("Skill root bindings are owner-issued")
        object.__setattr__(self, "root_kind", root_kind)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "containment_root", containment_root)
        object.__setattr__(self, "location_prefix", location_prefix)
        object.__setattr__(self, "precedence_ordinal", precedence_ordinal)
        object.__setattr__(self, "_owner_authority", _owner_authority)
        self.__post_init__()

    def __post_init__(self) -> None:
        if not isinstance(self.root_kind, LocalSkillRootKind):
            raise TypeError("Skill root kind is not closed")
        if not self.path.is_absolute() or not self.containment_root.is_absolute():
            raise ValueError("Skill root binding is not absolute")
        if self.location_prefix != _ROOT_LOCATION_PREFIX[self.root_kind]:
            raise ValueError("Skill root location prefix conflicts")
        if self.precedence_ordinal != _ROOT_ORDER.index(self.root_kind):
            raise ValueError("Skill root precedence conflicts")


@dataclass(frozen=True, slots=True, init=False)
class PreparedLocalSkillRootPolicy:
    """Scope-neutral physical roots issued by one LocalSkillProvider."""

    selected_root_kinds: tuple[LocalSkillRootKind, ...]
    roots: tuple[PreparedSkillRootBinding, ...]
    configuration_unavailable_reason: PulsaraHomeUnavailableReason | None
    _owner_authority: object = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        selected_root_kinds: tuple[LocalSkillRootKind, ...],
        roots: tuple[PreparedSkillRootBinding, ...],
        configuration_unavailable_reason: PulsaraHomeUnavailableReason | None,
        _owner_authority: object,
        _constructor: object,
    ) -> None:
        if _constructor is not _ROOT_POLICY_CONSTRUCTOR:
            raise TypeError("Skill root policies are owner-issued")
        object.__setattr__(self, "selected_root_kinds", selected_root_kinds)
        object.__setattr__(self, "roots", roots)
        object.__setattr__(
            self,
            "configuration_unavailable_reason",
            configuration_unavailable_reason,
        )
        object.__setattr__(self, "_owner_authority", _owner_authority)
        self.__post_init__()

    def __post_init__(self) -> None:
        if self.selected_root_kinds != tuple(
            sorted(self.selected_root_kinds, key=_ROOT_ORDER.index)
        ) or len(self.selected_root_kinds) != len(set(self.selected_root_kinds)):
            raise ValueError("Skill root selection is not ordered and unique")
        if len(self.selected_root_kinds) > len(_ROOT_ORDER):
            raise ValueError("Skill root policy exceeds the closed root set")
        kinds = tuple(item.root_kind for item in self.roots)
        if (
            kinds != tuple(item for item in self.selected_root_kinds if item in kinds)
            or len(kinds) != len(set(kinds))
            or len({item.path for item in self.roots}) != len(self.roots)
            or len({item.location_prefix for item in self.roots}) != len(self.roots)
        ):
            raise ValueError("Skill root bindings are not ordered and unique")
        if any(
            item._owner_authority is not self._owner_authority for item in self.roots
        ):
            raise ValueError("Skill root policy contains a foreign binding")
        missing = set(self.selected_root_kinds) - set(kinds)
        if bool(missing) != (self.configuration_unavailable_reason is not None):
            raise ValueError("Skill root configuration state conflicts")
        if missing and missing != {LocalSkillRootKind.USER_PULSARA}:
            raise ValueError("unexpected unresolved Skill root binding")

    @property
    def excluded_root_kinds(self) -> tuple[LocalSkillRootKind, ...]:
        selected = set(self.selected_root_kinds)
        return tuple(item for item in _ROOT_ORDER if item not in selected)


@dataclass(frozen=True, slots=True)
class InvalidLocalSkillCandidateIssue:
    path: Path
    root_kind: LocalSkillRootKind
    diagnostics: tuple[SkillDiagnostic, ...]
    declared_name: str | None = None
    kind: LocalSkillCandidateIssueKind = field(
        default=LocalSkillCandidateIssueKind.INVALID, init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.root_kind, LocalSkillRootKind):
            raise TypeError("invalid candidate root kind is not closed")
        if not self.diagnostics or any(
            item.path != self.path for item in self.diagnostics
        ):
            raise ValueError("invalid candidate diagnostics are incomplete")


@dataclass(frozen=True, slots=True)
class ShadowedLocalSkillCandidateIssue:
    path: Path
    root_kind: LocalSkillRootKind
    name: str
    winner_ordinal: int
    local_diagnostic_codes: tuple[SkillDiagnosticCode, ...] = ()
    kind: LocalSkillCandidateIssueKind = field(
        default=LocalSkillCandidateIssueKind.SHADOWED, init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.root_kind, LocalSkillRootKind):
            raise TypeError("shadowed candidate root kind is not closed")
        if not self.name or self.winner_ordinal < 0:
            raise ValueError("shadowed candidate identity is incomplete")
        if any(not _is_local_info_code(item) for item in self.local_diagnostic_codes):
            raise ValueError("shadowed candidate contains a non-local diagnostic")


LocalSkillCandidateIssue: TypeAlias = (
    InvalidLocalSkillCandidateIssue | ShadowedLocalSkillCandidateIssue
)


@dataclass(frozen=True, slots=True)
class LocalSkillWinnerDiagnostics:
    winner_ordinal: int
    diagnostic_codes: tuple[SkillDiagnosticCode, ...]

    def __post_init__(self) -> None:
        if self.winner_ordinal < 0 or not self.diagnostic_codes:
            raise ValueError("winner-local diagnostics are incomplete")
        allowed = {
            SkillDiagnosticCode.HOST_EXTENSION_IGNORED,
            SkillDiagnosticCode.UNKNOWN_EXTENSION_IGNORED,
        }
        if any(item not in allowed for item in self.diagnostic_codes):
            raise ValueError("winner-local diagnostic ownership conflicts")


@dataclass(frozen=True, slots=True)
class LocalSkillDiscovery:
    """The sole Runtime/management inspection carrier."""

    root_policy: PreparedLocalSkillRootPolicy = field(repr=False)
    disposition: SkillDiscoveryDisposition
    skills: tuple[LocalSkillManifest, ...] = ()
    candidate_issues: tuple[LocalSkillCandidateIssue, ...] = ()
    winner_local_diagnostics: tuple[LocalSkillWinnerDiagnostics, ...] = ()
    unavailable_reason: SkillCatalogUnavailableReason | None = None
    unavailable_diagnostics: tuple[SkillDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.root_policy, PreparedLocalSkillRootPolicy):
            raise TypeError("Skill discovery root policy is not frozen")
        if not isinstance(self.disposition, SkillDiscoveryDisposition):
            raise TypeError("Skill discovery disposition is not closed")
        complete = self.disposition is SkillDiscoveryDisposition.COMPLETE
        if complete != (self.unavailable_reason is None):
            raise ValueError("Skill discovery disposition/reason conflicts")
        if complete and self.unavailable_diagnostics:
            raise ValueError("complete discovery contains aggregate diagnostics")
        if not complete and (
            self.skills or self.candidate_issues or self.winner_local_diagnostics
        ):
            raise ValueError("unavailable Skill discovery contains partial facts")
        if not complete and not self.unavailable_diagnostics:
            raise ValueError("unavailable discovery has no aggregate diagnostic")
        if len(self.skills) > MAX_ADMITTED_SKILLS:
            raise ValueError("Skill discovery admitted too many manifests")
        names = tuple(item.name for item in self.skills)
        if len(names) != len(set(names)):
            raise ValueError("Skill discovery contains duplicate winners")
        issue_keys = tuple(
            _candidate_issue_sort_key(item) for item in self.candidate_issues
        )
        if issue_keys != tuple(sorted(issue_keys)):
            raise ValueError("Skill candidate issues are not deterministic")
        for issue in self.candidate_issues:
            if isinstance(issue, ShadowedLocalSkillCandidateIssue):
                if issue.winner_ordinal >= len(self.skills):
                    raise ValueError(
                        "shadowed candidate winner ordinal is outside discovery"
                    )
                if self.skills[issue.winner_ordinal].name != issue.name:
                    raise ValueError("shadowed candidate winner name conflicts")
        ordinals = tuple(item.winner_ordinal for item in self.winner_local_diagnostics)
        if ordinals != tuple(sorted(ordinals)) or len(ordinals) != len(set(ordinals)):
            raise ValueError("winner-local diagnostics are not ordered and unique")
        if any(
            item.winner_ordinal >= len(self.skills)
            for item in self.winner_local_diagnostics
        ):
            raise ValueError("winner-local diagnostic ordinal is outside discovery")


class _DiscoveryUnavailable(RuntimeError):
    def __init__(
        self,
        reason: SkillCatalogUnavailableReason,
        code: SkillDiagnosticCode,
    ) -> None:
        super().__init__(code.value)
        self.reason = reason
        self.code = code


@dataclass(frozen=True, slots=True)
class _DiscoveryChildEvidence:
    name: str
    file_type: int
    device: int
    inode: int
    skill_file_identity: tuple[int, int, int, int, int] | None = None

    def __post_init__(self) -> None:
        if not self.name or self.name.startswith("."):
            raise ValueError("discovery child evidence is not visible")
        is_directory = self.file_type == stat.S_IFDIR
        if (self.skill_file_identity is not None) and not is_directory:
            raise ValueError("non-directory discovery child has a Skill document")


@dataclass(frozen=True, slots=True)
class _HeldDiscoveryChild:
    evidence: _DiscoveryChildEvidence
    descriptor: int | None

    def __post_init__(self) -> None:
        is_directory = self.evidence.file_type == stat.S_IFDIR
        if self.descriptor is not None and not is_directory:
            raise ValueError("non-directory discovery child has a descriptor")


@dataclass(frozen=True, slots=True)
class _HeldDiscoveryRoot:
    binding: PreparedSkillRootBinding
    descriptor: int | None
    device: int | None
    inode: int | None
    children: tuple[_HeldDiscoveryChild, ...] = ()

    def __post_init__(self) -> None:
        present = self.descriptor is not None
        if present != (self.device is not None and self.inode is not None):
            raise ValueError("discovery root identity conflicts with its descriptor")
        if not present and self.children:
            raise ValueError("absent discovery root has child evidence")


class _DuplicateYamlKey(ValueError):
    pass


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise _DuplicateYamlKey("duplicate YAML mapping key")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class LocalSkillProvider:
    """The sole physical root-policy, scan, and precedence owner."""

    def __init__(
        self,
        *,
        max_skill_file_bytes: int = MAX_SKILL_FILE_BYTES,
        user_product_skills_root: Path | None = None,
        user_agents_skills_root: Path | None = None,
        pulsara_home_resolution: PulsaraHomeResolution | None = None,
        include_user_skills: bool = True,
        maximum_direct_child_directories: int = MAX_SKILL_DIRECT_CHILDREN,
        maximum_admitted_skills: int = MAX_ADMITTED_SKILLS,
        maximum_discovery_skill_bytes: int = MAX_DISCOVERY_SKILL_BYTES,
    ) -> None:
        if not 1 <= max_skill_file_bytes <= MAX_SKILL_FILE_BYTES:
            raise ValueError("Skill file bound is outside the closed maximum")
        if not 1 <= maximum_direct_child_directories <= MAX_SKILL_DIRECT_CHILDREN:
            raise ValueError("Skill direct-child bound is outside the closed maximum")
        if not 1 <= maximum_admitted_skills <= MAX_ADMITTED_SKILLS:
            raise ValueError("Skill winner bound is outside the closed maximum")
        if not 1 <= maximum_discovery_skill_bytes <= MAX_DISCOVERY_SKILL_BYTES:
            raise ValueError("Skill discovery byte bound is outside the closed maximum")
        self.max_skill_file_bytes = max_skill_file_bytes
        self.user_product_skills_root = user_product_skills_root
        self.user_agents_skills_root = user_agents_skills_root
        self.pulsara_home_resolution = pulsara_home_resolution
        self.include_user_skills = include_user_skills
        self.maximum_direct_child_directories = maximum_direct_child_directories
        self.maximum_admitted_skills = maximum_admitted_skills
        self.maximum_discovery_skill_bytes = maximum_discovery_skill_bytes
        self._owner_authority = object()

    def prepare_root_policy(
        self,
        workspace_root: Path,
        *,
        root_kinds: tuple[LocalSkillRootKind, ...] | None = None,
    ) -> PreparedLocalSkillRootPolicy:
        workspace = workspace_root.expanduser().resolve()
        selected = root_kinds
        if selected is None:
            selected = _ROOT_ORDER if self.include_user_skills else _ROOT_ORDER[:2]
        if selected != tuple(sorted(selected, key=_ROOT_ORDER.index)) or len(
            selected
        ) != len(set(selected)):
            raise ValueError("Skill root selection is not ordered and unique")
        home_resolution = self.pulsara_home_resolution
        if (
            LocalSkillRootKind.USER_PULSARA in selected
            and self.user_product_skills_root is None
            and home_resolution is None
        ):
            home_resolution = resolve_pulsara_home()
        configuration_reason: PulsaraHomeUnavailableReason | None = None
        roots: list[PreparedSkillRootBinding] = []
        for root_kind in selected:
            if (
                root_kind is LocalSkillRootKind.USER_PULSARA
                and self.user_product_skills_root is None
                and home_resolution is not None
                and home_resolution.disposition is PulsaraHomeDisposition.INVALID
            ):
                configuration_reason = home_resolution.unavailable_reason
                continue
            roots.append(
                self._prepare_root_binding(
                    workspace,
                    root_kind,
                    home_resolution=home_resolution,
                )
            )
        return PreparedLocalSkillRootPolicy(
            selected_root_kinds=selected,
            roots=tuple(roots),
            configuration_unavailable_reason=configuration_reason,
            _owner_authority=self._owner_authority,
            _constructor=_ROOT_POLICY_CONSTRUCTOR,
        )

    def discover(
        self,
        policy: PreparedLocalSkillRootPolicy,
        *,
        deadline_monotonic: float | None = None,
    ) -> LocalSkillDiscovery:
        if (
            type(policy) is not PreparedLocalSkillRootPolicy
            or policy._owner_authority is not self._owner_authority
        ):
            raise ValueError("foreign Skill root policy")
        if policy.configuration_unavailable_reason is not None:
            return _unavailable_discovery(
                policy,
                SkillCatalogUnavailableReason.USER_HOME_CONFIGURATION_INVALID,
                SkillDiagnosticCode.USER_HOME_CONFIGURATION_INVALID,
            )

        held_roots: list[_HeldDiscoveryRoot] = []
        try:
            _check_discovery_deadline(deadline_monotonic)
            for root in policy.roots:
                held_roots.append(
                    _observe_discovery_root(
                        root,
                        maximum_direct_child_directories=(
                            self.maximum_direct_child_directories
                        ),
                        deadline_monotonic=deadline_monotonic,
                    )
                )
        except TimeoutError:
            _close_discovery_roots(held_roots)
            return _unavailable_discovery(
                policy,
                SkillCatalogUnavailableReason.DISCOVERY_RACED,
                SkillDiagnosticCode.DISCOVERY_DEADLINE_EXPIRED,
            )
        except _DiscoveryUnavailable as exc:
            _close_discovery_roots(held_roots)
            return _unavailable_discovery(policy, exc.reason, exc.code)
        except (MemoryError, OSError, RuntimeError):
            _close_discovery_roots(held_roots)
            return _unavailable_discovery(
                policy,
                SkillCatalogUnavailableReason.DISCOVERY_RACED,
                SkillDiagnosticCode.ENUMERATION_RACED,
            )
        try:
            skills: list[LocalSkillManifest] = []
            issues: list[LocalSkillCandidateIssue] = []
            winner_local: list[LocalSkillWinnerDiagnostics] = []
            winners_by_name: dict[str, int] = {}
            observed_bytes = 0
            for held_root in held_roots:
                for child in held_root.children:
                    if child.evidence.skill_file_identity is None:
                        continue
                    _check_discovery_deadline(deadline_monotonic)
                    data = _read_discovery_skill_document(
                        child,
                        maximum=self.max_skill_file_bytes,
                        deadline_monotonic=deadline_monotonic,
                    )
                    observed_bytes += len(data)
                    if observed_bytes > self.maximum_discovery_skill_bytes:
                        raise _DiscoveryUnavailable(
                            SkillCatalogUnavailableReason.DISCOVERY_OVERBOUND,
                            SkillDiagnosticCode.DISCOVERY_BYTE_BOUND_EXCEEDED,
                        )
                    root = held_root.binding
                    skill_file = root.path / child.evidence.name / SKILL_FILE_NAME
                    parsed = parse_local_skill_document(
                        data,
                        expected_directory_name=child.evidence.name,
                        maximum_file_bytes=self.max_skill_file_bytes,
                    )
                    diagnostics = tuple(
                        _diagnostic_at(item, skill_file) for item in parsed.diagnostics
                    )
                    if parsed.parsed is None:
                        issues.append(
                            InvalidLocalSkillCandidateIssue(
                                path=skill_file,
                                root_kind=root.root_kind,
                                diagnostics=diagnostics,
                                declared_name=parsed.declared_name,
                            )
                        )
                        continue
                    location = _skill_location(skill_file, root=root)
                    if len(location.encode("utf-8")) > MAX_SKILL_LOCATION_BYTES:
                        issues.append(
                            InvalidLocalSkillCandidateIssue(
                                path=skill_file,
                                root_kind=root.root_kind,
                                diagnostics=(
                                    _diagnostic(
                                        SkillDiagnosticSeverity.WARNING,
                                        SkillDiagnosticCode.LOCATION_OVERBOUND,
                                        path=skill_file,
                                    ),
                                ),
                                declared_name=parsed.parsed.name,
                            )
                        )
                        continue
                    winner_ordinal = winners_by_name.get(parsed.parsed.name)
                    local_codes = tuple(item.code for item in diagnostics)
                    if winner_ordinal is not None:
                        issues.append(
                            ShadowedLocalSkillCandidateIssue(
                                path=skill_file,
                                root_kind=root.root_kind,
                                name=parsed.parsed.name,
                                winner_ordinal=winner_ordinal,
                                local_diagnostic_codes=local_codes,
                            )
                        )
                        continue
                    manifest = enrich_local_skill_document(
                        parsed.parsed,
                        path=skill_file,
                        root=root,
                    )
                    winners_by_name[manifest.name] = len(skills)
                    skills.append(manifest)
                    ignored_codes = tuple(
                        item
                        for item in local_codes
                        if item
                        in {
                            SkillDiagnosticCode.HOST_EXTENSION_IGNORED,
                            SkillDiagnosticCode.UNKNOWN_EXTENSION_IGNORED,
                        }
                    )
                    if ignored_codes:
                        winner_local.append(
                            LocalSkillWinnerDiagnostics(
                                winner_ordinal=len(skills) - 1,
                                diagnostic_codes=ignored_codes,
                            )
                        )
                    if len(skills) > self.maximum_admitted_skills:
                        raise _DiscoveryUnavailable(
                            SkillCatalogUnavailableReason.CATALOG_OVERBOUND,
                            SkillDiagnosticCode.WINNER_BOUND_EXCEEDED,
                        )
            _check_discovery_deadline(deadline_monotonic)
            try:
                render_catalog_prompt(
                    tuple(
                        ResolvedSkillCatalogEntry(
                            name=item.name,
                            description=item.description,
                            location=item.location,
                            source=item.source,
                        )
                        for item in skills
                    )
                )
            except SkillProjectionOverbound as exc:
                raise _DiscoveryUnavailable(
                    SkillCatalogUnavailableReason.CATALOG_OVERBOUND,
                    SkillDiagnosticCode.CATALOG_PROJECTION_BOUND_EXCEEDED,
                ) from exc
            _revalidate_discovery_roots(
                held_roots,
                maximum_direct_child_directories=(
                    self.maximum_direct_child_directories
                ),
                deadline_monotonic=deadline_monotonic,
            )
            return LocalSkillDiscovery(
                root_policy=policy,
                disposition=SkillDiscoveryDisposition.COMPLETE,
                skills=tuple(skills),
                candidate_issues=tuple(sorted(issues, key=_candidate_issue_sort_key)),
                winner_local_diagnostics=tuple(winner_local),
            )
        except TimeoutError:
            return _unavailable_discovery(
                policy,
                SkillCatalogUnavailableReason.DISCOVERY_RACED,
                SkillDiagnosticCode.DISCOVERY_DEADLINE_EXPIRED,
            )
        except _DiscoveryUnavailable as exc:
            return _unavailable_discovery(policy, exc.reason, exc.code)
        except (MemoryError, OSError, RuntimeError):
            return _unavailable_discovery(
                policy,
                SkillCatalogUnavailableReason.DISCOVERY_RACED,
                SkillDiagnosticCode.READ_RACED,
            )
        finally:
            _close_discovery_roots(held_roots)

    def _prepare_root_binding(
        self,
        workspace_root: Path,
        root_kind: LocalSkillRootKind,
        *,
        home_resolution: PulsaraHomeResolution | None,
    ) -> PreparedSkillRootBinding:
        if root_kind is LocalSkillRootKind.WORKSPACE_PULSARA:
            path = workspace_root.joinpath(*WORKSPACE_PRODUCT_SKILL_ROOT_PARTS)
            containment = workspace_root
        elif root_kind is LocalSkillRootKind.WORKSPACE_AGENTS:
            path = workspace_root.joinpath(*WORKSPACE_AGENTS_SKILL_ROOT_PARTS)
            containment = workspace_root
        elif root_kind is LocalSkillRootKind.USER_PULSARA:
            if self.user_product_skills_root is not None:
                path = self.user_product_skills_root
            elif home_resolution is not None and home_resolution.path is not None:
                path = home_resolution.path / "skills"
            else:
                raise ValueError("resolved Pulsara home is required")
            containment = path
        elif root_kind is LocalSkillRootKind.USER_AGENTS:
            path = self.user_agents_skills_root or Path.home().joinpath(
                *USER_AGENTS_SKILL_ROOT_PARTS
            )
            containment = path
        else:  # pragma: no cover - closed enum
            raise TypeError("unknown Skill root kind")
        path = path.expanduser().resolve()
        containment = containment.expanduser().resolve()
        return PreparedSkillRootBinding(
            root_kind=root_kind,
            path=path,
            containment_root=containment,
            location_prefix=_ROOT_LOCATION_PREFIX[root_kind],
            precedence_ordinal=_ROOT_ORDER.index(root_kind),
            _owner_authority=self._owner_authority,
            _constructor=_ROOT_POLICY_CONSTRUCTOR,
        )


def parse_local_skill_document(
    data: bytes,
    *,
    expected_directory_name: str,
    maximum_file_bytes: int = MAX_SKILL_FILE_BYTES,
) -> SkillDocumentParseResult:
    """Parse exact bytes without root, scope, provenance, or filesystem writes."""

    if (
        not expected_directory_name
        or expected_directory_name in {".", ".."}
        or "/" in expected_directory_name
        or os.sep in expected_directory_name
    ):
        raise ValueError("expected Skill directory basename is invalid")
    if not 1 <= maximum_file_bytes <= MAX_SKILL_FILE_BYTES:
        raise ValueError("Skill file bound is outside the closed maximum")
    diagnostics: list[SkillDiagnostic] = []
    if len(data) > maximum_file_bytes:
        return SkillDocumentParseResult(
            None,
            None,
            (
                _diagnostic(
                    SkillDiagnosticSeverity.WARNING,
                    SkillDiagnosticCode.DOCUMENT_OVERBOUND,
                ),
            ),
        )
    try:
        document = data.decode("utf-8")
    except UnicodeDecodeError:
        return SkillDocumentParseResult(
            None,
            None,
            (
                _diagnostic(
                    SkillDiagnosticSeverity.ERROR, SkillDiagnosticCode.INVALID_UTF8
                ),
            ),
        )
    frontmatter, body = _extract_frontmatter(document)
    if frontmatter is None or body is None:
        return SkillDocumentParseResult(
            None,
            None,
            (
                _diagnostic(
                    SkillDiagnosticSeverity.WARNING,
                    SkillDiagnosticCode.MISSING_FRONTMATTER,
                ),
            ),
        )
    if len(frontmatter.encode("utf-8")) > MAX_SKILL_FRONTMATTER_BYTES:
        return SkillDocumentParseResult(
            None,
            None,
            (
                _diagnostic(
                    SkillDiagnosticSeverity.WARNING,
                    SkillDiagnosticCode.FRONTMATTER_OVERBOUND,
                ),
            ),
        )
    try:
        _validate_yaml_shape(frontmatter)
        raw_fields = _load_unique_yaml_mapping(frontmatter)
    except (ValueError, yaml.YAMLError, _DuplicateYamlKey):
        return SkillDocumentParseResult(
            None,
            None,
            (
                _diagnostic(
                    SkillDiagnosticSeverity.WARNING,
                    SkillDiagnosticCode.INVALID_FRONTMATTER_YAML,
                ),
            ),
        )
    unsupported = tuple(sorted(set(raw_fields) - _STANDARD_FIELDS))
    for key in unsupported:
        code = (
            SkillDiagnosticCode.HOST_EXTENSION_IGNORED
            if key in _HOST_EXTENSION_FIELDS
            else SkillDiagnosticCode.UNKNOWN_EXTENSION_IGNORED
        )
        diagnostics.append(_diagnostic(SkillDiagnosticSeverity.INFO, code))

    name = _required_trimmed_string(raw_fields, "name")
    description = _required_trimmed_string(raw_fields, "description")
    invalid = False
    if (
        name is None
        or len(name.encode("utf-8")) > MAX_SKILL_NAME_BYTES
        or _NAME_RE.fullmatch(name) is None
    ):
        diagnostics.append(
            _diagnostic(
                SkillDiagnosticSeverity.WARNING, SkillDiagnosticCode.INVALID_NAME
            )
        )
        invalid = True
    elif name != expected_directory_name:
        diagnostics.append(
            _diagnostic(
                SkillDiagnosticSeverity.WARNING,
                SkillDiagnosticCode.DIRECTORY_NAME_MISMATCH,
            )
        )
        invalid = True
    if description is None or not 1 <= len(description) <= MAX_SKILL_DESCRIPTION_CHARS:
        diagnostics.append(
            _diagnostic(
                SkillDiagnosticSeverity.WARNING,
                SkillDiagnosticCode.INVALID_DESCRIPTION,
            )
        )
        invalid = True
    license_value = _optional_trimmed_string(raw_fields, "license")
    if "license" in raw_fields and (
        license_value is None
        or len(license_value.encode("utf-8")) > MAX_SKILL_LICENSE_BYTES
    ):
        diagnostics.append(
            _diagnostic(
                SkillDiagnosticSeverity.WARNING, SkillDiagnosticCode.INVALID_LICENSE
            )
        )
        invalid = True
    compatibility = _optional_trimmed_string(raw_fields, "compatibility")
    if "compatibility" in raw_fields and (
        compatibility is None
        or not 1 <= len(compatibility) <= MAX_SKILL_COMPATIBILITY_CHARS
    ):
        diagnostics.append(
            _diagnostic(
                SkillDiagnosticSeverity.WARNING,
                SkillDiagnosticCode.INVALID_COMPATIBILITY,
            )
        )
        invalid = True
    if "metadata" in raw_fields:
        metadata, metadata_valid = _parse_metadata(raw_fields["metadata"])
    else:
        metadata, metadata_valid = (), True
    if not metadata_valid:
        diagnostics.append(
            _diagnostic(
                SkillDiagnosticSeverity.WARNING, SkillDiagnosticCode.INVALID_METADATA
            )
        )
        invalid = True
    if invalid or name is None or description is None:
        return SkillDocumentParseResult(None, name, tuple(diagnostics))

    authoring: list[SkillAuthoringDiagnosticCode] = []
    if len(body.splitlines()) > 500:
        authoring.append(SkillAuthoringDiagnosticCode.BODY_OVER_500_LINES)
        diagnostics.append(
            _diagnostic(
                SkillDiagnosticSeverity.INFO,
                SkillDiagnosticCode.BODY_OVER_500_LINES,
            )
        )
    raw_digest = "sha256:" + sha256(data).hexdigest()
    semantic = local_skill_manifest_semantic_fingerprint(
        name=name,
        description=description,
        license=license_value,
        compatibility=compatibility,
        metadata=metadata,
        body=body,
    )
    return SkillDocumentParseResult(
        ParsedLocalSkillDocument(
            name=name,
            description=description,
            license=license_value,
            compatibility=compatibility,
            metadata=metadata,
            body=body,
            raw_document_digest=raw_digest,
            manifest_semantic_fingerprint=semantic,
            authoring_diagnostic_codes=tuple(authoring),
            raw_document=document,
        ),
        name,
        tuple(diagnostics),
    )


def enrich_local_skill_document(
    parsed: ParsedLocalSkillDocument,
    *,
    path: Path,
    root: PreparedSkillRootBinding,
) -> LocalSkillManifest:
    if path.name != SKILL_FILE_NAME or path.parent.parent != root.path:
        raise ValueError("Skill placement does not match its owner-issued root")
    if path.parent.name != parsed.name:
        raise ValueError("Skill placement basename conflicts with parsed document")
    location = _skill_location(path, root=root)
    if len(location.encode("utf-8")) > MAX_SKILL_LOCATION_BYTES:
        raise ValueError("Skill placement location is overbound")
    return LocalSkillManifest(
        name=parsed.name,
        description=parsed.description,
        license=parsed.license,
        compatibility=parsed.compatibility,
        metadata=parsed.metadata,
        path=path,
        base_dir=path.parent,
        location=location,
        body=parsed.body,
        raw_document_digest=parsed.raw_document_digest,
        manifest_semantic_fingerprint=parsed.manifest_semantic_fingerprint,
        root_kind=root.root_kind,
        authoring_diagnostic_codes=parsed.authoring_diagnostic_codes,
        raw_document=parsed.raw_document,
    )


def local_skill_manifest_semantic_fingerprint(
    *,
    name: str,
    description: str,
    license: str | None,
    compatibility: str | None,
    metadata: tuple[tuple[str, str], ...],
    body: str,
) -> str:
    return context_fingerprint(
        "local-skill-manifest-semantic:v2-agent-skills-core",
        {
            "contract": AGENT_SKILLS_CONTRACT_ID,
            "name": name,
            "description": description,
            "license": license,
            "compatibility": compatibility,
            "metadata": metadata,
            "body": body,
        },
    )


def discovery_diagnostics(
    discovery: LocalSkillDiscovery,
) -> tuple[SkillDiagnostic, ...]:
    """Derive a projection without storing a second diagnostics truth."""

    if discovery.disposition is SkillDiscoveryDisposition.UNAVAILABLE:
        return discovery.unavailable_diagnostics
    result: list[SkillDiagnostic] = []
    for issue in discovery.candidate_issues:
        if isinstance(issue, InvalidLocalSkillCandidateIssue):
            result.extend(issue.diagnostics)
            continue
        result.append(
            _diagnostic(
                SkillDiagnosticSeverity.WARNING,
                SkillDiagnosticCode.DUPLICATE_NAME,
                path=issue.path,
            )
        )
        result.extend(
            _diagnostic(SkillDiagnosticSeverity.INFO, code, path=issue.path)
            for code in issue.local_diagnostic_codes
        )
    for ordinal, skill in enumerate(discovery.skills):
        result.extend(
            _diagnostic(
                SkillDiagnosticSeverity.INFO,
                SkillDiagnosticCode(item.value),
                path=skill.path,
            )
            for item in skill.authoring_diagnostic_codes
        )
        winner = next(
            (
                item
                for item in discovery.winner_local_diagnostics
                if item.winner_ordinal == ordinal
            ),
            None,
        )
        if winner is not None:
            result.extend(
                _diagnostic(SkillDiagnosticSeverity.INFO, code, path=skill.path)
                for code in winner.diagnostic_codes
            )
    return tuple(result)


def _validate_yaml_shape(frontmatter: str) -> None:
    node_count = 0
    depth = 0
    maximum_depth = 0
    documents = 0
    for event in yaml.parse(frontmatter, Loader=yaml.SafeLoader):
        if isinstance(event, DocumentStartEvent):
            documents += 1
            if documents > 1:
                raise ValueError("multi-document YAML is forbidden")
        if isinstance(event, AliasEvent) or getattr(event, "anchor", None) is not None:
            raise ValueError("YAML anchors and aliases are forbidden")
        if getattr(event, "tag", None) is not None:
            raise ValueError("explicit YAML tags are forbidden")
        if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
            node_count += 1
            depth += 1
            maximum_depth = max(maximum_depth, depth)
        elif isinstance(event, ScalarEvent):
            node_count += 1
        elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
            depth -= 1
        if node_count > MAX_SKILL_YAML_NODES or maximum_depth > MAX_SKILL_YAML_DEPTH:
            raise ValueError("YAML physical shape exceeds its bound")
    if documents != 1 or depth != 0:
        raise ValueError("YAML document shape is incomplete")


def _load_unique_yaml_mapping(frontmatter: str) -> dict[str, Any]:
    loader = _UniqueKeySafeLoader(frontmatter)
    try:
        parsed = loader.get_single_data()
    finally:
        loader.dispose()
    if not isinstance(parsed, dict) or any(not isinstance(key, str) for key in parsed):
        raise ValueError("Skill frontmatter must be a string-keyed mapping")
    return parsed


def _extract_frontmatter(document: str) -> tuple[str | None, str | None]:
    lines = document.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None, None
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---" and not line[:1].isspace():
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    return None, None


def _parse_metadata(raw: Any) -> tuple[tuple[tuple[str, str], ...], bool]:
    if not isinstance(raw, dict) or len(raw) > MAX_SKILL_METADATA_ITEMS:
        return (), False
    values: list[tuple[str, str]] = []
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return (), False
        key_bytes = len(key.encode("utf-8"))
        value_bytes = len(value.encode("utf-8"))
        if (
            not 1 <= key_bytes <= MAX_SKILL_METADATA_KEY_BYTES
            or value_bytes > MAX_SKILL_METADATA_VALUE_BYTES
        ):
            return (), False
        values.append((key, value))
    frozen = tuple(sorted(values))
    if len(canonical_json_bytes(frozen)) > MAX_SKILL_METADATA_BYTES:
        return (), False
    return frozen, True


def _required_trimmed_string(fields: dict[str, Any], key: str) -> str | None:
    value = fields.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _optional_trimmed_string(fields: dict[str, Any], key: str) -> str | None:
    if key not in fields:
        return None
    return _required_trimmed_string(fields, key)


def _observe_discovery_root(
    binding: PreparedSkillRootBinding,
    *,
    maximum_direct_child_directories: int,
    deadline_monotonic: float | None,
) -> _HeldDiscoveryRoot:
    try:
        binding.path.relative_to(binding.containment_root)
    except ValueError as exc:
        raise _DiscoveryUnavailable(
            SkillCatalogUnavailableReason.DISCOVERY_RACED,
            SkillDiagnosticCode.ROOT_ESCAPE,
        ) from exc
    _check_discovery_deadline(deadline_monotonic)
    try:
        descriptor = open_absolute_directory_nofollow(binding.path)
    except FileNotFoundError:
        return _HeldDiscoveryRoot(binding, None, None, None)
    except NotADirectoryError as exc:
        raise _DiscoveryUnavailable(
            SkillCatalogUnavailableReason.DISCOVERY_RACED,
            SkillDiagnosticCode.ROOT_NOT_DIRECTORY,
        ) from exc
    except OSError as exc:
        raise _DiscoveryUnavailable(
            SkillCatalogUnavailableReason.DISCOVERY_RACED,
            SkillDiagnosticCode.ENUMERATION_RACED,
        ) from exc
    children: tuple[_HeldDiscoveryChild, ...] = ()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise _DiscoveryUnavailable(
                SkillCatalogUnavailableReason.DISCOVERY_RACED,
                SkillDiagnosticCode.ROOT_NOT_DIRECTORY,
            )
        children = _snapshot_discovery_children(
            descriptor,
            maximum_direct_child_directories=maximum_direct_child_directories,
            deadline_monotonic=deadline_monotonic,
            retain_descriptors=True,
            overbound_reason=SkillCatalogUnavailableReason.DISCOVERY_OVERBOUND,
            overbound_code=SkillDiagnosticCode.DIRECT_CHILD_BOUND_EXCEEDED,
        )
        return _HeldDiscoveryRoot(
            binding,
            descriptor,
            metadata.st_dev,
            metadata.st_ino,
            children,
        )
    except BaseException:
        _close_discovery_children(children)
        os.close(descriptor)
        raise


def _snapshot_discovery_children(
    root_fd: int,
    *,
    maximum_direct_child_directories: int,
    deadline_monotonic: float | None,
    retain_descriptors: bool,
    overbound_reason: SkillCatalogUnavailableReason,
    overbound_code: SkillDiagnosticCode,
) -> tuple[_HeldDiscoveryChild, ...]:
    try:
        names = tuple(
            sorted(name for name in os.listdir(root_fd) if not name.startswith("."))
        )
    except OSError as exc:
        raise _DiscoveryUnavailable(
            SkillCatalogUnavailableReason.DISCOVERY_RACED,
            SkillDiagnosticCode.ENUMERATION_RACED,
        ) from exc
    children: list[_HeldDiscoveryChild] = []
    directory_count = 0
    try:
        for name in names:
            _check_discovery_deadline(deadline_monotonic)
            try:
                metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except OSError as exc:
                raise _DiscoveryUnavailable(
                    SkillCatalogUnavailableReason.DISCOVERY_RACED,
                    SkillDiagnosticCode.ENUMERATION_RACED,
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise _DiscoveryUnavailable(
                    SkillCatalogUnavailableReason.DISCOVERY_RACED,
                    SkillDiagnosticCode.DIRECTORY_ESCAPE,
                )
            file_type = stat.S_IFMT(metadata.st_mode)
            if file_type != stat.S_IFDIR:
                children.append(
                    _HeldDiscoveryChild(
                        _DiscoveryChildEvidence(
                            name,
                            file_type,
                            metadata.st_dev,
                            metadata.st_ino,
                        ),
                        None,
                    )
                )
                continue
            directory_count += 1
            if directory_count > maximum_direct_child_directories:
                raise _DiscoveryUnavailable(overbound_reason, overbound_code)
            try:
                child_fd = os.open(name, _DISCOVERY_DIRECTORY_FLAGS, dir_fd=root_fd)
            except OSError as exc:
                raise _DiscoveryUnavailable(
                    SkillCatalogUnavailableReason.DISCOVERY_RACED,
                    SkillDiagnosticCode.ENUMERATION_RACED,
                ) from exc
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    raise _DiscoveryUnavailable(
                        SkillCatalogUnavailableReason.DISCOVERY_RACED,
                        SkillDiagnosticCode.ENUMERATION_RACED,
                    )
                skill_identity = _discovery_skill_file_identity(child_fd)
                children.append(
                    _HeldDiscoveryChild(
                        _DiscoveryChildEvidence(
                            name,
                            file_type,
                            metadata.st_dev,
                            metadata.st_ino,
                            skill_identity,
                        ),
                        child_fd if retain_descriptors else None,
                    )
                )
                if retain_descriptors:
                    child_fd = -1
            finally:
                if child_fd >= 0:
                    os.close(child_fd)
        return tuple(children)
    except BaseException:
        _close_discovery_children(tuple(children))
        raise


def _discovery_skill_file_identity(
    child_fd: int,
) -> tuple[int, int, int, int, int] | None:
    try:
        metadata = os.stat(SKILL_FILE_NAME, dir_fd=child_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _DiscoveryUnavailable(
            SkillCatalogUnavailableReason.DISCOVERY_RACED,
            SkillDiagnosticCode.ENUMERATION_RACED,
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise _DiscoveryUnavailable(
            SkillCatalogUnavailableReason.DISCOVERY_RACED,
            SkillDiagnosticCode.FILE_ESCAPE,
        )
    if not stat.S_ISREG(metadata.st_mode):
        raise _DiscoveryUnavailable(
            SkillCatalogUnavailableReason.DISCOVERY_RACED,
            SkillDiagnosticCode.READ_RACED,
        )
    return _file_identity(metadata)


def _read_discovery_skill_document(
    child: _HeldDiscoveryChild,
    *,
    maximum: int,
    deadline_monotonic: float | None,
) -> bytes:
    descriptor = child.descriptor
    expected = child.evidence.skill_file_identity
    if descriptor is None or expected is None:
        raise ValueError("discovery candidate has no held Skill document")
    _check_discovery_deadline(deadline_monotonic)
    try:
        file_fd = os.open(SKILL_FILE_NAME, _DISCOVERY_FILE_FLAGS, dir_fd=descriptor)
    except OSError as exc:
        raise _DiscoveryUnavailable(
            SkillCatalogUnavailableReason.DISCOVERY_RACED,
            SkillDiagnosticCode.READ_RACED,
        ) from exc
    try:
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or _file_identity(before) != expected:
            raise _DiscoveryUnavailable(
                SkillCatalogUnavailableReason.DISCOVERY_RACED,
                SkillDiagnosticCode.READ_RACED,
            )
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            _check_discovery_deadline(deadline_monotonic)
            try:
                chunk = os.read(file_fd, remaining)
            except OSError as exc:
                raise _DiscoveryUnavailable(
                    SkillCatalogUnavailableReason.DISCOVERY_RACED,
                    SkillDiagnosticCode.READ_RACED,
                ) from exc
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if _file_identity(os.fstat(file_fd)) != expected:
            raise _DiscoveryUnavailable(
                SkillCatalogUnavailableReason.DISCOVERY_RACED,
                SkillDiagnosticCode.READ_RACED,
            )
        return b"".join(chunks)
    finally:
        os.close(file_fd)


def _revalidate_discovery_roots(
    roots: list[_HeldDiscoveryRoot],
    *,
    maximum_direct_child_directories: int,
    deadline_monotonic: float | None,
) -> None:
    for observed in roots:
        _check_discovery_deadline(deadline_monotonic)
        if observed.descriptor is None:
            try:
                appeared = open_absolute_directory_nofollow(observed.binding.path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise _DiscoveryUnavailable(
                    SkillCatalogUnavailableReason.DISCOVERY_RACED,
                    SkillDiagnosticCode.ENUMERATION_RACED,
                ) from exc
            else:
                os.close(appeared)
                raise _DiscoveryUnavailable(
                    SkillCatalogUnavailableReason.DISCOVERY_RACED,
                    SkillDiagnosticCode.ENUMERATION_RACED,
                )
        try:
            held_metadata = os.fstat(observed.descriptor)
            rebound = open_absolute_directory_nofollow(observed.binding.path)
        except OSError as exc:
            raise _DiscoveryUnavailable(
                SkillCatalogUnavailableReason.DISCOVERY_RACED,
                SkillDiagnosticCode.ENUMERATION_RACED,
            ) from exc
        current_children: tuple[_HeldDiscoveryChild, ...] = ()
        try:
            rebound_metadata = os.fstat(rebound)
            expected_root = (observed.device, observed.inode)
            if (held_metadata.st_dev, held_metadata.st_ino) != expected_root or (
                rebound_metadata.st_dev,
                rebound_metadata.st_ino,
            ) != expected_root:
                raise _DiscoveryUnavailable(
                    SkillCatalogUnavailableReason.DISCOVERY_RACED,
                    SkillDiagnosticCode.ENUMERATION_RACED,
                )
            for child in observed.children:
                if child.descriptor is None:
                    continue
                child_metadata = os.fstat(child.descriptor)
                if (child_metadata.st_dev, child_metadata.st_ino) != (
                    child.evidence.device,
                    child.evidence.inode,
                ):
                    raise _DiscoveryUnavailable(
                        SkillCatalogUnavailableReason.DISCOVERY_RACED,
                        SkillDiagnosticCode.ENUMERATION_RACED,
                    )
            current_children = _snapshot_discovery_children(
                rebound,
                maximum_direct_child_directories=(maximum_direct_child_directories),
                deadline_monotonic=deadline_monotonic,
                retain_descriptors=False,
                overbound_reason=SkillCatalogUnavailableReason.DISCOVERY_RACED,
                overbound_code=SkillDiagnosticCode.ENUMERATION_RACED,
            )
            if tuple(item.evidence for item in current_children) != tuple(
                item.evidence for item in observed.children
            ):
                raise _DiscoveryUnavailable(
                    SkillCatalogUnavailableReason.DISCOVERY_RACED,
                    SkillDiagnosticCode.ENUMERATION_RACED,
                )
        finally:
            _close_discovery_children(current_children)
            os.close(rebound)


def _close_discovery_children(children: tuple[_HeldDiscoveryChild, ...]) -> None:
    for child in children:
        if child.descriptor is None:
            continue
        try:
            os.close(child.descriptor)
        except OSError:
            pass


def _close_discovery_roots(roots: list[_HeldDiscoveryRoot]) -> None:
    for root in roots:
        _close_discovery_children(root.children)
        if root.descriptor is None:
            continue
        try:
            os.close(root.descriptor)
        except OSError:
            pass


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _skill_location(path: Path, *, root: PreparedSkillRootBinding) -> str:
    if path.name != SKILL_FILE_NAME or path.parent.parent != root.path:
        raise ValueError("Skill candidate does not match its frozen lexical root")
    relative = path.relative_to(root.path).as_posix()
    return f"{root.location_prefix}/{relative}"


def _check_discovery_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
        raise TimeoutError("local Skill discovery deadline expired")


def _candidate_issue_sort_key(
    issue: LocalSkillCandidateIssue,
) -> tuple[int, str, str]:
    return (
        _ROOT_ORDER.index(issue.root_kind),
        issue.path.as_posix(),
        issue.kind.value,
    )


def _is_local_info_code(code: SkillDiagnosticCode) -> bool:
    return code in {
        SkillDiagnosticCode.HOST_EXTENSION_IGNORED,
        SkillDiagnosticCode.UNKNOWN_EXTENSION_IGNORED,
        SkillDiagnosticCode.BODY_OVER_500_LINES,
        SkillDiagnosticCode.BODY_ESTIMATE_OVER_5000_TOKENS,
    }


def _diagnostic_at(item: SkillDiagnostic, path: Path) -> SkillDiagnostic:
    return SkillDiagnostic(
        severity=item.severity,
        code=item.code,
        message=item.message,
        path=path,
    )


def _diagnostic(
    severity: SkillDiagnosticSeverity,
    code: SkillDiagnosticCode,
    *,
    path: Path | None = None,
) -> SkillDiagnostic:
    return SkillDiagnostic(
        severity=severity,
        code=code,
        message=_DIAGNOSTIC_MESSAGES[code],
        path=path,
    )


_DIAGNOSTIC_MESSAGES = {
    code: code.value.replace("skill_", "").replace("_", " ")
    for code in SkillDiagnosticCode
}
_DIAGNOSTIC_MESSAGES[SkillDiagnosticCode.ACTIVE_SKILL_NOT_FOUND] = (
    "One or more requested Skills were not found"
)


def _unavailable_discovery(
    policy: PreparedLocalSkillRootPolicy,
    reason: SkillCatalogUnavailableReason,
    code: SkillDiagnosticCode,
) -> LocalSkillDiscovery:
    return LocalSkillDiscovery(
        root_policy=policy,
        disposition=SkillDiscoveryDisposition.UNAVAILABLE,
        unavailable_reason=reason,
        unavailable_diagnostics=(_diagnostic(SkillDiagnosticSeverity.ERROR, code),),
    )


__all__ = [
    "AGENT_SKILLS_CONTRACT_ID",
    "BUNDLED_SKILL_PROVENANCE_FILE_NAME",
    "InvalidLocalSkillCandidateIssue",
    "LocalSkillCandidateIssue",
    "LocalSkillCandidateIssueKind",
    "LocalSkillDiscovery",
    "LocalSkillProvider",
    "LocalSkillWinnerDiagnostics",
    "MAX_SKILL_FILE_BYTES",
    "ParsedLocalSkillDocument",
    "PreparedLocalSkillRootPolicy",
    "PreparedSkillRootBinding",
    "SKILL_FILE_NAME",
    "ShadowedLocalSkillCandidateIssue",
    "SkillDiscoveryDisposition",
    "SkillDocumentParseResult",
    "discovery_diagnostics",
    "enrich_local_skill_document",
    "local_skill_manifest_semantic_fingerprint",
    "parse_local_skill_document",
]
