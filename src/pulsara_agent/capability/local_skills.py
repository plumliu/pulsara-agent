"""Source-neutral Skill parser and the sole four-root loose definition producer."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
from time import monotonic
from typing import Any, Protocol

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
from pulsara_agent.local_source_binding import (
    open_absolute_directory_nofollow,
    prepare_local_source_path,
)
from pulsara_agent.capability.pulsara_home import (
    PulsaraHomeDisposition,
    PulsaraHomeResolution,
    PulsaraHomeUnavailableReason,
    UserHomeResolution,
    resolve_pulsara_home,
    resolve_user_home,
)
from pulsara_agent.capability.types import (
    InvalidSkillCandidateIssue,
    LooseSkillOrigin,
    ProducerUnavailableCause,
    SkillAuthoringDiagnosticCode,
    SkillDiagnostic,
    SkillDiagnosticCode,
    SkillDiagnosticSeverity,
    SkillDefinitionOrigin,
    SkillManifest,
    SkillProducerKind,
    SkillProducerUnavailableReason,
)
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint


WORKSPACE_PRODUCT_SKILL_ROOT_PARTS = (".pulsara", "skills")
WORKSPACE_AGENTS_SKILL_ROOT_PARTS = (".agents", "skills")
USER_AGENTS_SKILL_ROOT_PARTS = (".agents", "skills")
USER_PRODUCT_LOCATION_PREFIX = "${PULSARA_HOME}/skills"
SKILL_FILE_NAME = "SKILL.md"

AGENT_SKILLS_CONTRACT_ID = "pulsara.agent-skills-core.v1"
SKILL_PLACEMENT_CONTRACT_ID = "pulsara.skill-placement.v1"
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
LOOSE_SKILL_ROOT_ORDER = (
    LocalSkillRootKind.WORKSPACE_PULSARA,
    LocalSkillRootKind.WORKSPACE_AGENTS,
    LocalSkillRootKind.USER_PULSARA,
    LocalSkillRootKind.USER_AGENTS,
)
LOOSE_SKILL_LOCATION_PREFIX = {
    LocalSkillRootKind.WORKSPACE_PULSARA: ".pulsara/skills",
    LocalSkillRootKind.WORKSPACE_AGENTS: ".agents/skills",
    LocalSkillRootKind.USER_PULSARA: USER_PRODUCT_LOCATION_PREFIX,
    LocalSkillRootKind.USER_AGENTS: "~/.agents/skills",
}
_ROOT_POLICY_CONSTRUCTOR = object()
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class LooseSkillDefinitionsDisposition(StrEnum):
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ParsedSkillDocument:
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
    parsed: ParsedSkillDocument | None
    declared_name: str | None
    diagnostics: tuple[SkillDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.parsed is None and not self.diagnostics:
            raise ValueError("invalid Skill parse result has no diagnostic")
        if self.parsed is not None and self.declared_name != self.parsed.name:
            raise ValueError("parsed Skill declared name conflicts")
        if any(item.path is not None for item in self.diagnostics):
            raise ValueError("root-neutral parser emitted a physical path")


@dataclass(frozen=True, slots=True)
class SkillPlacementValidationResult:
    valid: bool
    diagnostics: tuple[SkillDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if self.valid == bool(self.diagnostics):
            raise ValueError("Skill placement result is inconsistent")
        if any(item.path is not None for item in self.diagnostics):
            raise ValueError("placement validator emitted a physical path")


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
        if self.location_prefix != LOOSE_SKILL_LOCATION_PREFIX[self.root_kind]:
            raise ValueError("Skill root location prefix conflicts")
        if self.precedence_ordinal != LOOSE_SKILL_ROOT_ORDER.index(self.root_kind):
            raise ValueError("Skill root precedence conflicts")


@dataclass(frozen=True, slots=True)
class LooseSkillRootAlias:
    first_root_kind: LocalSkillRootKind
    first_path: Path
    second_root_kind: LocalSkillRootKind
    second_path: Path
    shared_device: int | None = None
    shared_inode: int | None = None

    def __post_init__(self) -> None:
        if self.first_root_kind == self.second_root_kind:
            raise ValueError("loose root alias must join two roots")
        if not self.first_path.is_absolute() or not self.second_path.is_absolute():
            raise ValueError("loose root alias paths are not absolute")
        if (self.shared_device is None) != (self.shared_inode is None):
            raise ValueError("loose root alias physical identity is incomplete")


@dataclass(frozen=True, slots=True, init=False)
class PreparedLooseSkillRootPolicy:
    """The exact, always-four-root loose definition policy."""

    roots: tuple[PreparedSkillRootBinding, ...]
    configuration_unavailable_reason: PulsaraHomeUnavailableReason | None
    lexical_aliases: tuple[LooseSkillRootAlias, ...]
    _owner_authority: object = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        roots: tuple[PreparedSkillRootBinding, ...],
        configuration_unavailable_reason: PulsaraHomeUnavailableReason | None,
        lexical_aliases: tuple[LooseSkillRootAlias, ...],
        _owner_authority: object,
        _constructor: object,
    ) -> None:
        if _constructor is not _ROOT_POLICY_CONSTRUCTOR:
            raise TypeError("Skill root policies are owner-issued")
        object.__setattr__(self, "roots", roots)
        object.__setattr__(
            self, "configuration_unavailable_reason", configuration_unavailable_reason
        )
        object.__setattr__(self, "lexical_aliases", lexical_aliases)
        object.__setattr__(self, "_owner_authority", _owner_authority)
        self.__post_init__()

    def __post_init__(self) -> None:
        kinds = tuple(item.root_kind for item in self.roots)
        expected = tuple(item for item in LOOSE_SKILL_ROOT_ORDER if item in kinds)
        if kinds != expected or len(kinds) != len(set(kinds)):
            raise ValueError("loose Skill root bindings are not ordered and unique")
        if any(
            item._owner_authority is not self._owner_authority for item in self.roots
        ):
            raise ValueError("loose Skill root policy contains a foreign binding")
        missing = set(LOOSE_SKILL_ROOT_ORDER) - set(kinds)
        if bool(missing) != (self.configuration_unavailable_reason is not None):
            raise ValueError("loose Skill root configuration state conflicts")
        if missing and not missing.issubset(
            {LocalSkillRootKind.USER_PULSARA, LocalSkillRootKind.USER_AGENTS}
        ):
            raise ValueError("unexpected unresolved loose Skill root binding")


@dataclass(frozen=True, slots=True)
class FrozenLooseSkillDefinitions:
    root_policy: PreparedLooseSkillRootPolicy = field(repr=False)
    disposition: LooseSkillDefinitionsDisposition
    candidates: tuple[SkillManifest, ...] = ()
    invalid_issues: tuple[InvalidSkillCandidateIssue, ...] = ()
    unavailable_cause: ProducerUnavailableCause | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.root_policy, PreparedLooseSkillRootPolicy):
            raise TypeError("loose Skill definitions lack a frozen root policy")
        complete = self.disposition is LooseSkillDefinitionsDisposition.COMPLETE
        if complete != (self.unavailable_cause is None):
            raise ValueError("loose Skill definitions disposition conflicts")
        if not complete and (self.candidates or self.invalid_issues):
            raise ValueError("unavailable loose definitions contain partial facts")
        if self.unavailable_cause is not None and (
            self.unavailable_cause.producer_kind is not SkillProducerKind.LOOSE
        ):
            raise ValueError("loose definitions contain a foreign cause")
        keys = tuple(_candidate_sort_key(item) for item in self.candidates)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("loose Skill candidates are not deterministic and unique")
        issue_keys = tuple(_invalid_issue_sort_key(item) for item in self.invalid_issues)
        if issue_keys != tuple(sorted(issue_keys)) or len(issue_keys) != len(
            set(issue_keys)
        ):
            raise ValueError("loose invalid issues are not deterministic and unique")
        if any(not isinstance(item.origin, LooseSkillOrigin) for item in self.candidates):
            raise ValueError("loose definitions contain a non-loose candidate")


class SkillObservationError(RuntimeError):
    def __init__(self, code: SkillDiagnosticCode, *, overbound: bool = False) -> None:
        super().__init__(code.value)
        self.code = code
        self.overbound = overbound


class SkillObservationCancellationPort(Protocol):
    def cancellation_requested(self) -> bool: ...


class SkillObservationCancelled(Exception):
    """Cooperative abort that must not become producer UNAVAILABLE."""


@dataclass(frozen=True, slots=True)
class ObservedSkillChildEvidence:
    name: str
    file_type: int
    device: int
    inode: int
    skill_file_identity: tuple[int, int, int, int, int] | None = None

    def __post_init__(self) -> None:
        if not self.name or self.name.startswith("."):
            raise ValueError("observed child evidence is not visible")
        if self.skill_file_identity is not None and self.file_type != stat.S_IFDIR:
            raise ValueError("non-directory child has a Skill document")


@dataclass(frozen=True, slots=True)
class HeldSkillChild:
    evidence: ObservedSkillChildEvidence
    descriptor: int | None


@dataclass(frozen=True, slots=True)
class HeldSkillRoot:
    path: Path
    containment_root: Path
    descriptor: int | None
    device: int | None
    inode: int | None
    children: tuple[HeldSkillChild, ...] = ()

    def __post_init__(self) -> None:
        present = self.descriptor is not None
        if present != (self.device is not None and self.inode is not None):
            raise ValueError("observed root identity conflicts")
        if not present and self.children:
            raise ValueError("absent observed root has child evidence")


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
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


class LooseSkillDefinitionProducer:
    """The sole four-root policy and complete loose observation owner."""

    def __init__(
        self,
        *,
        max_skill_file_bytes: int = MAX_SKILL_FILE_BYTES,
        user_product_skills_root: Path | None = None,
        user_agents_skills_root: Path | None = None,
        pulsara_home_resolution: PulsaraHomeResolution | None = None,
        user_home_resolution: UserHomeResolution | None = None,
        maximum_direct_child_directories: int = MAX_SKILL_DIRECT_CHILDREN,
        maximum_discovery_skill_bytes: int = MAX_DISCOVERY_SKILL_BYTES,
    ) -> None:
        if not 1 <= max_skill_file_bytes <= MAX_SKILL_FILE_BYTES:
            raise ValueError("Skill file bound is outside the closed maximum")
        if not 1 <= maximum_direct_child_directories <= MAX_SKILL_DIRECT_CHILDREN:
            raise ValueError("Skill direct-child bound is outside the closed maximum")
        if not 1 <= maximum_discovery_skill_bytes <= MAX_DISCOVERY_SKILL_BYTES:
            raise ValueError("Skill discovery byte bound is outside the closed maximum")
        self.max_skill_file_bytes = max_skill_file_bytes
        self.user_product_skills_root = user_product_skills_root
        self.user_agents_skills_root = user_agents_skills_root
        self.pulsara_home_resolution = pulsara_home_resolution
        self.user_home_resolution = user_home_resolution
        self.maximum_direct_child_directories = maximum_direct_child_directories
        self.maximum_discovery_skill_bytes = maximum_discovery_skill_bytes
        self._owner_authority = object()

    def prepare_root_policy(self, workspace_root: Path) -> PreparedLooseSkillRootPolicy:
        workspace = prepare_local_source_path(workspace_root)
        user_home_resolution = self.user_home_resolution
        if self.user_agents_skills_root is None and user_home_resolution is None:
            user_home_resolution = resolve_user_home()
        home_resolution = self.pulsara_home_resolution
        if self.user_product_skills_root is None and home_resolution is None:
            home_resolution = resolve_pulsara_home(
                user_home_resolution=user_home_resolution
            )
        configuration_reason: PulsaraHomeUnavailableReason | None = None
        roots: list[PreparedSkillRootBinding] = []
        for root_kind in LOOSE_SKILL_ROOT_ORDER:
            if (
                root_kind is LocalSkillRootKind.USER_PULSARA
                and self.user_product_skills_root is None
                and home_resolution is not None
                and home_resolution.disposition is PulsaraHomeDisposition.INVALID
            ):
                configuration_reason = home_resolution.unavailable_reason
                continue
            if (
                root_kind is LocalSkillRootKind.USER_AGENTS
                and self.user_agents_skills_root is None
                and user_home_resolution is not None
                and user_home_resolution.disposition is PulsaraHomeDisposition.INVALID
            ):
                configuration_reason = (
                    configuration_reason
                    or user_home_resolution.unavailable_reason
                    or PulsaraHomeUnavailableReason.USER_HOME_UNAVAILABLE
                )
                continue
            roots.append(
                self._prepare_root_binding(
                    workspace,
                    root_kind,
                    home_resolution=home_resolution,
                    user_home_resolution=user_home_resolution,
                )
            )
        aliases: list[LooseSkillRootAlias] = []
        for ordinal, first in enumerate(roots):
            for second in roots[ordinal + 1 :]:
                if first.path == second.path:
                    aliases.append(
                        LooseSkillRootAlias(
                            first.root_kind,
                            first.path,
                            second.root_kind,
                            second.path,
                        )
                    )
        return PreparedLooseSkillRootPolicy(
            roots=tuple(roots),
            configuration_unavailable_reason=configuration_reason,
            lexical_aliases=tuple(aliases),
            _owner_authority=self._owner_authority,
            _constructor=_ROOT_POLICY_CONSTRUCTOR,
        )

    def observe(
        self,
        policy: PreparedLooseSkillRootPolicy,
        *,
        deadline_monotonic: float | None = None,
        cancellation: SkillObservationCancellationPort | None = None,
    ) -> FrozenLooseSkillDefinitions:
        if (
            type(policy) is not PreparedLooseSkillRootPolicy
            or policy._owner_authority is not self._owner_authority
        ):
            raise ValueError("foreign loose Skill root policy")
        check_skill_deadline(deadline_monotonic, cancellation)
        if policy.configuration_unavailable_reason is not None:
            return _unavailable_loose_definitions(
                policy,
                SkillProducerUnavailableReason.LOOSE_CONFIGURATION_INVALID,
                (
                    skill_diagnostic(
                        SkillDiagnosticSeverity.ERROR,
                        SkillDiagnosticCode.USER_HOME_CONFIGURATION_INVALID,
                    ),
                ),
            )
        if policy.lexical_aliases:
            return _unavailable_loose_definitions(
                policy,
                SkillProducerUnavailableReason.LOOSE_CONFIGURATION_INVALID,
                tuple(_root_alias_diagnostic(item) for item in policy.lexical_aliases),
            )

        followed_aliases = _followed_root_aliases(policy)
        if followed_aliases:
            return _unavailable_loose_definitions(
                policy,
                SkillProducerUnavailableReason.LOOSE_CONFIGURATION_INVALID,
                tuple(_root_alias_diagnostic(item) for item in followed_aliases),
            )

        held_roots: list[HeldSkillRoot] = []
        try:
            for root in policy.roots:
                held_roots.append(
                    observe_skill_root(
                        root.path,
                        root.containment_root,
                        maximum_direct_child_directories=(
                            self.maximum_direct_child_directories
                        ),
                        deadline_monotonic=deadline_monotonic,
                        cancellation=cancellation,
                    )
                )
            inode_aliases = _physical_root_aliases(policy, held_roots)
            if inode_aliases:
                return _unavailable_loose_definitions(
                    policy,
                    SkillProducerUnavailableReason.LOOSE_CONFIGURATION_INVALID,
                    tuple(_root_alias_diagnostic(item) for item in inode_aliases),
                )

            candidates: list[SkillManifest] = []
            issues: list[InvalidSkillCandidateIssue] = []
            observed_bytes = 0
            for binding, held_root in zip(policy.roots, held_roots, strict=True):
                for child in held_root.children:
                    if child.evidence.skill_file_identity is None:
                        continue
                    check_skill_deadline(deadline_monotonic, cancellation)
                    data = read_observed_skill_document(
                        child,
                        maximum=self.max_skill_file_bytes,
                        deadline_monotonic=deadline_monotonic,
                        cancellation=cancellation,
                    )
                    observed_bytes += len(data)
                    if observed_bytes > self.maximum_discovery_skill_bytes:
                        raise SkillObservationError(
                            SkillDiagnosticCode.DISCOVERY_BYTE_BOUND_EXCEEDED,
                            overbound=True,
                        )
                    skill_file = binding.path / child.evidence.name / SKILL_FILE_NAME
                    parsed = parse_skill_document(
                        data, maximum_file_bytes=self.max_skill_file_bytes
                    )
                    placement = (
                        validate_skill_candidate_placement(
                            parsed.parsed, child.evidence.name
                        )
                        if parsed.parsed is not None
                        else None
                    )
                    diagnostics = tuple(
                        diagnostic_at(item, skill_file)
                        for item in (
                            *parsed.diagnostics,
                            *((placement.diagnostics) if placement is not None else ()),
                        )
                    )
                    if parsed.parsed is None or (
                        placement is not None and not placement.valid
                    ):
                        issues.append(
                            InvalidSkillCandidateIssue(
                                path=skill_file,
                                origin=LooseSkillOrigin(binding.root_kind),
                                diagnostics=diagnostics,
                                declared_name=parsed.declared_name,
                            )
                        )
                        continue
                    location = skill_location(skill_file, root=binding)
                    if len(location.encode("utf-8")) > MAX_SKILL_LOCATION_BYTES:
                        issues.append(
                            InvalidSkillCandidateIssue(
                                path=skill_file,
                                origin=LooseSkillOrigin(binding.root_kind),
                                diagnostics=(
                                    skill_diagnostic(
                                        SkillDiagnosticSeverity.WARNING,
                                        SkillDiagnosticCode.LOCATION_OVERBOUND,
                                        path=skill_file,
                                    ),
                                ),
                                declared_name=parsed.parsed.name,
                            )
                        )
                        continue
                    candidates.append(
                        enrich_skill_document(
                            parsed.parsed,
                            path=skill_file,
                            location=location,
                            origin=LooseSkillOrigin(binding.root_kind),
                            diagnostic_codes=tuple(item.code for item in diagnostics),
                        )
                    )
            revalidate_skill_roots(
                held_roots,
                maximum_direct_child_directories=(
                    self.maximum_direct_child_directories
                ),
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            )
            check_skill_deadline(deadline_monotonic, cancellation)
            return FrozenLooseSkillDefinitions(
                root_policy=policy,
                disposition=LooseSkillDefinitionsDisposition.COMPLETE,
                candidates=tuple(sorted(candidates, key=_candidate_sort_key)),
                invalid_issues=tuple(sorted(issues, key=_invalid_issue_sort_key)),
            )
        except TimeoutError:
            raise
        except SkillObservationError as exc:
            return _unavailable_loose_definitions(
                policy,
                (
                    SkillProducerUnavailableReason.LOOSE_DISCOVERY_OVERBOUND
                    if exc.overbound
                    else SkillProducerUnavailableReason.LOOSE_DISCOVERY_RACED
                ),
                (skill_diagnostic(SkillDiagnosticSeverity.ERROR, exc.code),),
            )
        except (MemoryError, OSError, RuntimeError):
            return _unavailable_loose_definitions(
                policy,
                SkillProducerUnavailableReason.LOOSE_DISCOVERY_RACED,
                (
                    skill_diagnostic(
                        SkillDiagnosticSeverity.ERROR, SkillDiagnosticCode.READ_RACED
                    ),
                ),
            )
        finally:
            close_skill_roots(held_roots)

    def _prepare_root_binding(
        self,
        workspace_root: Path,
        root_kind: LocalSkillRootKind,
        *,
        home_resolution: PulsaraHomeResolution | None,
        user_home_resolution: UserHomeResolution | None,
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
            if self.user_agents_skills_root is not None:
                path = self.user_agents_skills_root
            elif user_home_resolution is not None and user_home_resolution.path is not None:
                path = user_home_resolution.path.joinpath(*USER_AGENTS_SKILL_ROOT_PARTS)
            else:
                raise ValueError("resolved user home is required")
            containment = path
        else:  # pragma: no cover - closed enum
            raise TypeError("unknown Skill root kind")
        path = prepare_local_source_path(path)
        containment = prepare_local_source_path(containment)
        return PreparedSkillRootBinding(
            root_kind=root_kind,
            path=path,
            containment_root=containment,
            location_prefix=LOOSE_SKILL_LOCATION_PREFIX[root_kind],
            precedence_ordinal=LOOSE_SKILL_ROOT_ORDER.index(root_kind),
            _owner_authority=self._owner_authority,
            _constructor=_ROOT_POLICY_CONSTRUCTOR,
        )


def parse_skill_document(
    data: bytes, *, maximum_file_bytes: int = MAX_SKILL_FILE_BYTES
) -> SkillDocumentParseResult:
    """Parse exact bytes without root, path, placement, or source knowledge."""

    if not 1 <= maximum_file_bytes <= MAX_SKILL_FILE_BYTES:
        raise ValueError("Skill file bound is outside the closed maximum")
    diagnostics: list[SkillDiagnostic] = []
    if len(data) > maximum_file_bytes:
        return SkillDocumentParseResult(
            None,
            None,
            (
                skill_diagnostic(
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
                skill_diagnostic(
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
                skill_diagnostic(
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
                skill_diagnostic(
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
                skill_diagnostic(
                    SkillDiagnosticSeverity.WARNING,
                    SkillDiagnosticCode.INVALID_FRONTMATTER_YAML,
                ),
            ),
        )
    for key in sorted(set(raw_fields) - _STANDARD_FIELDS):
        diagnostics.append(
            skill_diagnostic(
                SkillDiagnosticSeverity.INFO,
                (
                    SkillDiagnosticCode.HOST_EXTENSION_IGNORED
                    if key in _HOST_EXTENSION_FIELDS
                    else SkillDiagnosticCode.UNKNOWN_EXTENSION_IGNORED
                ),
            )
        )

    name = _required_trimmed_string(raw_fields, "name")
    description = _required_trimmed_string(raw_fields, "description")
    invalid = False
    if (
        name is None
        or len(name.encode("utf-8")) > MAX_SKILL_NAME_BYTES
        or _NAME_RE.fullmatch(name) is None
    ):
        diagnostics.append(
            skill_diagnostic(
                SkillDiagnosticSeverity.WARNING, SkillDiagnosticCode.INVALID_NAME
            )
        )
        invalid = True
    if description is None or not 1 <= len(description) <= MAX_SKILL_DESCRIPTION_CHARS:
        diagnostics.append(
            skill_diagnostic(
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
            skill_diagnostic(
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
            skill_diagnostic(
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
            skill_diagnostic(
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
            skill_diagnostic(
                SkillDiagnosticSeverity.INFO, SkillDiagnosticCode.BODY_OVER_500_LINES
            )
        )
    raw_digest = "sha256:" + sha256(data).hexdigest()
    semantic = skill_manifest_semantic_fingerprint(
        name=name,
        description=description,
        license=license_value,
        compatibility=compatibility,
        metadata=metadata,
        body=body,
    )
    return SkillDocumentParseResult(
        ParsedSkillDocument(
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


def validate_skill_candidate_placement(
    parsed: ParsedSkillDocument, expected_immediate_child_name: str
) -> SkillPlacementValidationResult:
    if not isinstance(parsed, ParsedSkillDocument):
        raise TypeError("Skill placement requires a parsed document")
    if (
        not expected_immediate_child_name
        or expected_immediate_child_name in {".", ".."}
        or "/" in expected_immediate_child_name
        or os.sep in expected_immediate_child_name
    ):
        raise ValueError("expected Skill directory basename is invalid")
    if parsed.name == expected_immediate_child_name:
        return SkillPlacementValidationResult(valid=True)
    return SkillPlacementValidationResult(
        valid=False,
        diagnostics=(
            skill_diagnostic(
                SkillDiagnosticSeverity.WARNING,
                SkillDiagnosticCode.DIRECTORY_NAME_MISMATCH,
            ),
        ),
    )


def enrich_skill_document(
    parsed: ParsedSkillDocument,
    *,
    path: Path,
    location: str,
    origin: SkillDefinitionOrigin,
    diagnostic_codes: tuple[SkillDiagnosticCode, ...] = (),
) -> SkillManifest:
    if path.name != SKILL_FILE_NAME or path.parent.name != parsed.name:
        raise ValueError("Skill placement conflicts with parsed document")
    if len(location.encode("utf-8")) > MAX_SKILL_LOCATION_BYTES:
        raise ValueError("Skill placement location is overbound")
    return SkillManifest(
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
        origin=origin,
        diagnostic_codes=tuple(dict.fromkeys(diagnostic_codes)),
        authoring_diagnostic_codes=parsed.authoring_diagnostic_codes,
        raw_document=parsed.raw_document,
    )


def skill_manifest_semantic_fingerprint(
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


def observe_skill_root(
    path: Path,
    containment_root: Path,
    *,
    maximum_direct_child_directories: int,
    deadline_monotonic: float | None,
    cancellation: SkillObservationCancellationPort | None = None,
    bound_descriptor: int | None = None,
    bound_identity: tuple[int, int] | None = None,
    classify_nonregular_skill_document_as_missing: bool = False,
) -> HeldSkillRoot:
    try:
        path.relative_to(containment_root)
    except ValueError as exc:
        raise SkillObservationError(SkillDiagnosticCode.ROOT_ESCAPE) from exc
    check_skill_deadline(deadline_monotonic, cancellation)
    try:
        descriptor = (
            open_absolute_directory_nofollow(path)
            if bound_descriptor is None
            else os.dup(bound_descriptor)
        )
    except FileNotFoundError:
        if bound_descriptor is not None:
            raise SkillObservationError(SkillDiagnosticCode.ENUMERATION_RACED)
        return HeldSkillRoot(path, containment_root, None, None, None)
    except NotADirectoryError as exc:
        raise SkillObservationError(SkillDiagnosticCode.ROOT_NOT_DIRECTORY) from exc
    except OSError as exc:
        raise SkillObservationError(SkillDiagnosticCode.ENUMERATION_RACED) from exc
    children: tuple[HeldSkillChild, ...] = ()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise SkillObservationError(SkillDiagnosticCode.ROOT_NOT_DIRECTORY)
        if bound_identity is not None and (metadata.st_dev, metadata.st_ino) != (
            bound_identity
        ):
            raise SkillObservationError(SkillDiagnosticCode.ENUMERATION_RACED)
        children = _snapshot_skill_children(
            descriptor,
            maximum_direct_child_directories=maximum_direct_child_directories,
            deadline_monotonic=deadline_monotonic,
            cancellation=cancellation,
            retain_descriptors=True,
            overbound_code=SkillDiagnosticCode.DIRECT_CHILD_BOUND_EXCEEDED,
            classify_nonregular_skill_document_as_missing=(
                classify_nonregular_skill_document_as_missing
            ),
        )
        return HeldSkillRoot(
            path,
            containment_root,
            descriptor,
            metadata.st_dev,
            metadata.st_ino,
            children,
        )
    except BaseException:
        _close_skill_children(children)
        os.close(descriptor)
        raise


def read_observed_skill_document(
    child: HeldSkillChild,
    *,
    maximum: int,
    deadline_monotonic: float | None,
    cancellation: SkillObservationCancellationPort | None = None,
) -> bytes:
    descriptor = child.descriptor
    expected = child.evidence.skill_file_identity
    if descriptor is None or expected is None:
        raise ValueError("observed candidate has no held Skill document")
    check_skill_deadline(deadline_monotonic, cancellation)
    try:
        file_fd = os.open(SKILL_FILE_NAME, _FILE_FLAGS, dir_fd=descriptor)
    except OSError as exc:
        raise SkillObservationError(SkillDiagnosticCode.READ_RACED) from exc
    try:
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or _file_identity(before) != expected:
            raise SkillObservationError(SkillDiagnosticCode.READ_RACED)
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            check_skill_deadline(deadline_monotonic, cancellation)
            try:
                chunk = os.read(file_fd, remaining)
            except OSError as exc:
                raise SkillObservationError(SkillDiagnosticCode.READ_RACED) from exc
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if _file_identity(os.fstat(file_fd)) != expected:
            raise SkillObservationError(SkillDiagnosticCode.READ_RACED)
        return b"".join(chunks)
    finally:
        os.close(file_fd)


def revalidate_skill_roots(
    roots: list[HeldSkillRoot],
    *,
    maximum_direct_child_directories: int,
    deadline_monotonic: float | None,
    cancellation: SkillObservationCancellationPort | None = None,
) -> None:
    for observed in roots:
        check_skill_deadline(deadline_monotonic, cancellation)
        if observed.descriptor is None:
            try:
                appeared = open_absolute_directory_nofollow(observed.path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SkillObservationError(
                    SkillDiagnosticCode.ENUMERATION_RACED
                ) from exc
            else:
                os.close(appeared)
                raise SkillObservationError(SkillDiagnosticCode.ENUMERATION_RACED)
        try:
            held_metadata = os.fstat(observed.descriptor)
            rebound = open_absolute_directory_nofollow(observed.path)
        except OSError as exc:
            raise SkillObservationError(SkillDiagnosticCode.ENUMERATION_RACED) from exc
        current_children: tuple[HeldSkillChild, ...] = ()
        try:
            rebound_metadata = os.fstat(rebound)
            expected_root = (observed.device, observed.inode)
            if (held_metadata.st_dev, held_metadata.st_ino) != expected_root or (
                rebound_metadata.st_dev,
                rebound_metadata.st_ino,
            ) != expected_root:
                raise SkillObservationError(SkillDiagnosticCode.ENUMERATION_RACED)
            for child in observed.children:
                if child.descriptor is None:
                    continue
                child_metadata = os.fstat(child.descriptor)
                if (child_metadata.st_dev, child_metadata.st_ino) != (
                    child.evidence.device,
                    child.evidence.inode,
                ):
                    raise SkillObservationError(SkillDiagnosticCode.ENUMERATION_RACED)
            current_children = _snapshot_skill_children(
                rebound,
                maximum_direct_child_directories=maximum_direct_child_directories,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
                retain_descriptors=False,
                overbound_code=SkillDiagnosticCode.ENUMERATION_RACED,
                classify_nonregular_skill_document_as_missing=False,
            )
            if tuple(item.evidence for item in current_children) != tuple(
                item.evidence for item in observed.children
            ):
                raise SkillObservationError(SkillDiagnosticCode.ENUMERATION_RACED)
        finally:
            _close_skill_children(current_children)
            os.close(rebound)


def close_skill_roots(roots: list[HeldSkillRoot]) -> None:
    for root in roots:
        _close_skill_children(root.children)
        if root.descriptor is None:
            continue
        try:
            os.close(root.descriptor)
        except OSError:
            pass


def check_skill_deadline(
    deadline_monotonic: float | None,
    cancellation: SkillObservationCancellationPort | None = None,
) -> None:
    if cancellation is not None and cancellation.cancellation_requested():
        raise SkillObservationCancelled
    if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
        raise TimeoutError("Skill observation owner deadline expired")


def skill_location(path: Path, *, root: PreparedSkillRootBinding) -> str:
    if path.name != SKILL_FILE_NAME or path.parent.parent != root.path:
        raise ValueError("Skill candidate does not match its frozen lexical root")
    return f"{root.location_prefix}/{path.relative_to(root.path).as_posix()}"


def diagnostic_at(item: SkillDiagnostic, path: Path) -> SkillDiagnostic:
    return SkillDiagnostic(item.severity, item.code, item.message, path)


def skill_diagnostic(
    severity: SkillDiagnosticSeverity,
    code: SkillDiagnosticCode,
    *,
    path: Path | None = None,
    message: str | None = None,
) -> SkillDiagnostic:
    return SkillDiagnostic(
        severity=severity,
        code=code,
        message=message or SKILL_DIAGNOSTIC_MESSAGES[code],
        path=path,
    )


SKILL_DIAGNOSTIC_MESSAGES = {
    code: code.value.replace("skill_", "").replace("_", " ")
    for code in SkillDiagnosticCode
}
SKILL_DIAGNOSTIC_MESSAGES[SkillDiagnosticCode.ACTIVE_SKILL_NOT_FOUND] = (
    "One or more requested Skills were not found"
)


def _snapshot_skill_children(
    root_fd: int,
    *,
    maximum_direct_child_directories: int,
    deadline_monotonic: float | None,
    cancellation: SkillObservationCancellationPort | None,
    retain_descriptors: bool,
    overbound_code: SkillDiagnosticCode,
    classify_nonregular_skill_document_as_missing: bool = False,
) -> tuple[HeldSkillChild, ...]:
    try:
        names = tuple(
            sorted(name for name in os.listdir(root_fd) if not name.startswith("."))
        )
    except OSError as exc:
        raise SkillObservationError(SkillDiagnosticCode.ENUMERATION_RACED) from exc
    children: list[HeldSkillChild] = []
    directory_count = 0
    try:
        for name in names:
            check_skill_deadline(deadline_monotonic, cancellation)
            try:
                metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except OSError as exc:
                raise SkillObservationError(
                    SkillDiagnosticCode.ENUMERATION_RACED
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise SkillObservationError(SkillDiagnosticCode.DIRECTORY_ESCAPE)
            file_type = stat.S_IFMT(metadata.st_mode)
            if file_type != stat.S_IFDIR:
                children.append(
                    HeldSkillChild(
                        ObservedSkillChildEvidence(
                            name, file_type, metadata.st_dev, metadata.st_ino
                        ),
                        None,
                    )
                )
                continue
            directory_count += 1
            if directory_count > maximum_direct_child_directories:
                raise SkillObservationError(overbound_code, overbound=True)
            try:
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=root_fd)
            except OSError as exc:
                raise SkillObservationError(
                    SkillDiagnosticCode.ENUMERATION_RACED
                ) from exc
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    raise SkillObservationError(SkillDiagnosticCode.ENUMERATION_RACED)
                skill_identity = _skill_file_identity(
                    child_fd,
                    classify_nonregular_as_missing=(
                        classify_nonregular_skill_document_as_missing
                    ),
                )
                children.append(
                    HeldSkillChild(
                        ObservedSkillChildEvidence(
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
        _close_skill_children(tuple(children))
        raise


def _skill_file_identity(
    child_fd: int, *, classify_nonregular_as_missing: bool
) -> tuple[int, int, int, int, int] | None:
    try:
        metadata = os.stat(SKILL_FILE_NAME, dir_fd=child_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SkillObservationError(SkillDiagnosticCode.ENUMERATION_RACED) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise SkillObservationError(SkillDiagnosticCode.FILE_ESCAPE)
    if not stat.S_ISREG(metadata.st_mode):
        if classify_nonregular_as_missing:
            return None
        raise SkillObservationError(SkillDiagnosticCode.READ_RACED)
    return _file_identity(metadata)


def _physical_root_aliases(
    policy: PreparedLooseSkillRootPolicy, roots: list[HeldSkillRoot]
) -> tuple[LooseSkillRootAlias, ...]:
    result: list[LooseSkillRootAlias] = []
    for ordinal, first in enumerate(roots):
        if first.descriptor is None:
            continue
        for second_ordinal, second in enumerate(roots[ordinal + 1 :], ordinal + 1):
            if second.descriptor is None:
                continue
            if (first.device, first.inode) == (second.device, second.inode):
                first_binding = policy.roots[ordinal]
                second_binding = policy.roots[second_ordinal]
                result.append(
                    LooseSkillRootAlias(
                        first_binding.root_kind,
                        first_binding.path,
                        second_binding.root_kind,
                        second_binding.path,
                        first.device,
                        first.inode,
                    )
                )
    return tuple(result)


def _followed_root_aliases(
    policy: PreparedLooseSkillRootPolicy,
) -> tuple[LooseSkillRootAlias, ...]:
    """Identify a symlink/bind alias without using it as scan authority.

    The actual producer still opens every component with ``O_NOFOLLOW``.  This
    preliminary observation exists only so two configured names for the same
    directory settle as the closed configuration outcome instead of allowing
    either name to become a source path.
    """

    identities: list[tuple[PreparedSkillRootBinding, tuple[int, int]] | None] = []
    for binding in policy.roots:
        try:
            metadata = os.stat(binding.path, follow_symlinks=True)
        except (FileNotFoundError, NotADirectoryError):
            identities.append(None)
            continue
        except OSError:
            identities.append(None)
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            identities.append(None)
            continue
        identities.append((binding, (metadata.st_dev, metadata.st_ino)))

    result: list[LooseSkillRootAlias] = []
    for ordinal, first in enumerate(identities):
        if first is None:
            continue
        for second in identities[ordinal + 1 :]:
            if second is None or first[1] != second[1]:
                continue
            result.append(
                LooseSkillRootAlias(
                    first[0].root_kind,
                    first[0].path,
                    second[0].root_kind,
                    second[0].path,
                    *first[1],
                )
            )
    return tuple(result)


def _root_alias_diagnostic(alias: LooseSkillRootAlias) -> SkillDiagnostic:
    return skill_diagnostic(
        SkillDiagnosticSeverity.ERROR,
        SkillDiagnosticCode.LOOSE_ROOT_ALIAS,
        path=alias.first_path,
        message=(
            f"Loose Skill roots {alias.first_root_kind.value} ({alias.first_path}) "
            f"and {alias.second_root_kind.value} ({alias.second_path}) alias"
        ),
    )


def _unavailable_loose_definitions(
    policy: PreparedLooseSkillRootPolicy,
    reason: SkillProducerUnavailableReason,
    diagnostics: tuple[SkillDiagnostic, ...],
) -> FrozenLooseSkillDefinitions:
    ordered = tuple(
        sorted(
            diagnostics,
            key=lambda item: (
                item.code.value,
                "" if item.path is None else item.path.as_posix(),
                item.message,
            ),
        )
    )
    return FrozenLooseSkillDefinitions(
        root_policy=policy,
        disposition=LooseSkillDefinitionsDisposition.UNAVAILABLE,
        unavailable_cause=ProducerUnavailableCause(
            SkillProducerKind.LOOSE, reason, ordered
        ),
    )


def _candidate_sort_key(item: SkillManifest) -> tuple[int, str, str]:
    if not isinstance(item.origin, LooseSkillOrigin):
        raise TypeError("loose candidate has foreign origin")
    return (
        LOOSE_SKILL_ROOT_ORDER.index(item.origin.root_kind),
        item.path.parent.name,
        item.path.as_posix(),
    )


def _invalid_issue_sort_key(
    item: InvalidSkillCandidateIssue,
) -> tuple[int, str, str]:
    if not isinstance(item.origin, LooseSkillOrigin):
        raise TypeError("loose invalid issue has foreign origin")
    return (
        LOOSE_SKILL_ROOT_ORDER.index(item.origin.root_kind),
        item.path.as_posix(),
        item.declared_name or "",
    )


def _close_skill_children(children: tuple[HeldSkillChild, ...]) -> None:
    for child in children:
        if child.descriptor is None:
            continue
        try:
            os.close(child.descriptor)
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
        if (
            not 1 <= len(key.encode("utf-8")) <= MAX_SKILL_METADATA_KEY_BYTES
            or len(value.encode("utf-8")) > MAX_SKILL_METADATA_VALUE_BYTES
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


__all__ = [
    "AGENT_SKILLS_CONTRACT_ID",
    "FrozenLooseSkillDefinitions",
    "HeldSkillChild",
    "HeldSkillRoot",
    "LOOSE_SKILL_LOCATION_PREFIX",
    "LOOSE_SKILL_ROOT_ORDER",
    "LooseSkillDefinitionProducer",
    "LooseSkillDefinitionsDisposition",
    "MAX_ADMITTED_SKILLS",
    "MAX_SKILL_DIRECT_CHILDREN",
    "MAX_SKILL_FILE_BYTES",
    "MAX_SKILL_LOCATION_BYTES",
    "ParsedSkillDocument",
    "PreparedLooseSkillRootPolicy",
    "PreparedSkillRootBinding",
    "SKILL_FILE_NAME",
    "SKILL_PLACEMENT_CONTRACT_ID",
    "SkillDocumentParseResult",
    "SkillObservationError",
    "SkillPlacementValidationResult",
    "check_skill_deadline",
    "close_skill_roots",
    "diagnostic_at",
    "enrich_skill_document",
    "observe_skill_root",
    "parse_skill_document",
    "read_observed_skill_document",
    "revalidate_skill_roots",
    "skill_diagnostic",
    "skill_manifest_semantic_fingerprint",
    "validate_skill_candidate_placement",
]
