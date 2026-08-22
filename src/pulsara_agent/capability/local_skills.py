"""Bounded Agent Skills discovery for the four Round 9 physical roots."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
from time import monotonic
from typing import Any

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
from pulsara_agent.capability.types import (
    LocalSkillManifest,
    ResolvedSkillCatalogEntry,
    SkillAuthoringDiagnosticCode,
    SkillCatalogUnavailableReason,
    SkillDiagnostic,
    SkillDiagnosticSeverity,
)
from pulsara_agent.capability.render import (
    SkillProjectionOverbound,
    render_catalog_prompt,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint


WORKSPACE_PRODUCT_SKILL_ROOT_PARTS = (".pulsara", "skills")
WORKSPACE_AGENTS_SKILL_ROOT_PARTS = (".agents", "skills")
USER_PRODUCT_SKILL_ROOT_PARTS = (".pulsara", "skills")
USER_AGENTS_SKILL_ROOT_PARTS = (".agents", "skills")
PULSARA_HOME_ENV = "PULSARA_HOME"
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
MAX_INTERNAL_DIAGNOSTICS = 128

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


class SkillDiscoveryDisposition(StrEnum):
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


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
        if self.location_prefix != _ROOT_LOCATION_PREFIX[self.root_kind]:
            raise ValueError("Skill root location prefix conflicts")
        if self.precedence_ordinal != _ROOT_ORDER.index(self.root_kind):
            raise ValueError("Skill root precedence conflicts")


@dataclass(frozen=True, slots=True, init=False)
class PreparedLocalSkillRootPolicy:
    conversation_scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    roots: tuple[PreparedSkillRootBinding, ...]
    _owner_authority: object = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        roots: tuple[PreparedSkillRootBinding, ...],
        _owner_authority: object,
        _constructor: object,
    ) -> None:
        if _constructor is not _ROOT_POLICY_CONSTRUCTOR:
            raise TypeError("Skill root policies are owner-issued")
        object.__setattr__(self, "conversation_scope_kind", conversation_scope_kind)
        object.__setattr__(self, "scope_subagent_task_id", scope_subagent_task_id)
        object.__setattr__(self, "roots", roots)
        object.__setattr__(self, "_owner_authority", _owner_authority)
        self.__post_init__()

    def __post_init__(self) -> None:
        _validate_scope(
            self.conversation_scope_kind, self.scope_subagent_task_id
        )
        if len(self.roots) > len(_ROOT_ORDER):
            raise ValueError("Skill root policy exceeds the closed root set")
        kinds = tuple(item.root_kind for item in self.roots)
        ordinals = tuple(item.precedence_ordinal for item in self.roots)
        if (
            kinds != tuple(sorted(kinds, key=_ROOT_ORDER.index))
            or len(kinds) != len(set(kinds))
            or len(ordinals) != len(set(ordinals))
            or len({item.path for item in self.roots}) != len(self.roots)
            or len({item.location_prefix for item in self.roots}) != len(self.roots)
        ):
            raise ValueError("Skill root policy is not ordered and unique")
        if any(item._owner_authority is not self._owner_authority for item in self.roots):
            raise ValueError("Skill root policy contains a foreign binding")


@dataclass(frozen=True, slots=True)
class LocalSkillDiscovery:
    skills: tuple[LocalSkillManifest, ...]
    diagnostics: tuple[SkillDiagnostic, ...]
    root_policy: PreparedLocalSkillRootPolicy = field(repr=False)
    disposition: SkillDiscoveryDisposition = SkillDiscoveryDisposition.COMPLETE
    unavailable_reason: SkillCatalogUnavailableReason | None = None
    enumerated_candidate_count: int = 0
    observed_utf8_bytes: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.root_policy, PreparedLocalSkillRootPolicy):
            raise TypeError("Skill discovery root policy is not frozen")
        if not isinstance(self.disposition, SkillDiscoveryDisposition):
            raise TypeError("Skill discovery disposition is not closed")
        if (
            self.disposition is SkillDiscoveryDisposition.COMPLETE
        ) != (self.unavailable_reason is None):
            raise ValueError("Skill discovery disposition/reason conflicts")
        if self.disposition is SkillDiscoveryDisposition.UNAVAILABLE and self.skills:
            raise ValueError("unavailable Skill discovery contains partial facts")
        if len(self.skills) > MAX_ADMITTED_SKILLS:
            raise ValueError("Skill discovery admitted too many manifests")
        names = tuple(item.name for item in self.skills)
        if len(names) != len(set(names)):
            raise ValueError("Skill discovery contains duplicate winners")
        if self.enumerated_candidate_count < 0 or self.observed_utf8_bytes < 0:
            raise ValueError("Skill discovery counters are invalid")


class _DuplicateYamlKey(ValueError):
    pass


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
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
    """The sole physical root-policy and bounded scanner owner."""

    def __init__(
        self,
        *,
        max_skill_file_bytes: int = MAX_SKILL_FILE_BYTES,
        user_product_skills_root: Path | None = None,
        user_agents_skills_root: Path | None = None,
        include_user_skills: bool = True,
        maximum_direct_child_directories: int = MAX_SKILL_DIRECT_CHILDREN,
        maximum_admitted_skills: int = MAX_ADMITTED_SKILLS,
        maximum_discovery_skill_bytes: int = MAX_DISCOVERY_SKILL_BYTES,
    ) -> None:
        if not 1 <= max_skill_file_bytes <= MAX_SKILL_FILE_BYTES:
            raise ValueError("Skill file bound is outside the closed maximum")
        if not (
            1
            <= maximum_direct_child_directories
            <= MAX_SKILL_DIRECT_CHILDREN
        ):
            raise ValueError("Skill direct-child bound is outside the closed maximum")
        if not 1 <= maximum_admitted_skills <= MAX_ADMITTED_SKILLS:
            raise ValueError("Skill winner bound is outside the closed maximum")
        if not (
            1
            <= maximum_discovery_skill_bytes
            <= MAX_DISCOVERY_SKILL_BYTES
        ):
            raise ValueError("Skill discovery byte bound is outside the closed maximum")
        self.max_skill_file_bytes = max_skill_file_bytes
        self.user_product_skills_root = user_product_skills_root
        self.user_agents_skills_root = user_agents_skills_root
        self.include_user_skills = include_user_skills
        self.maximum_direct_child_directories = maximum_direct_child_directories
        self.maximum_admitted_skills = maximum_admitted_skills
        self.maximum_discovery_skill_bytes = maximum_discovery_skill_bytes
        self._owner_authority = object()

    def prepare_root_policy(
        self,
        workspace_root: Path,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
        root_kinds: tuple[LocalSkillRootKind, ...] | None = None,
    ) -> PreparedLocalSkillRootPolicy:
        _validate_scope(conversation_scope_kind, scope_subagent_task_id)
        workspace = workspace_root.expanduser().resolve()
        selected = root_kinds
        if selected is None:
            selected = _ROOT_ORDER if self.include_user_skills else _ROOT_ORDER[:2]
        if selected != tuple(sorted(selected, key=_ROOT_ORDER.index)) or len(
            selected
        ) != len(set(selected)):
            raise ValueError("Skill root selection is not ordered and unique")
        roots = tuple(
            self._prepare_root_binding(workspace, root_kind) for root_kind in selected
        )
        return PreparedLocalSkillRootPolicy(
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            roots=roots,
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
        diagnostics: list[SkillDiagnostic] = []
        candidates: list[tuple[PreparedSkillRootBinding, Path]] = []
        try:
            _check_discovery_deadline(deadline_monotonic)
            for root in policy.roots:
                _check_discovery_deadline(deadline_monotonic)
                try:
                    root_metadata = root.path.stat()
                except FileNotFoundError:
                    continue
                if not _is_within(root.path, root.containment_root):
                    return _unavailable_discovery(
                        policy,
                        SkillCatalogUnavailableReason.DISCOVERY_RACED,
                        "skill_root_escape",
                        diagnostics,
                    )
                if not stat.S_ISDIR(root_metadata.st_mode):
                    return _unavailable_discovery(
                        policy,
                        SkillCatalogUnavailableReason.DISCOVERY_RACED,
                        "skill_root_not_directory",
                        diagnostics,
                    )
                child_directories: list[Path] = []
                for child in root.path.iterdir():
                    _check_discovery_deadline(deadline_monotonic)
                    if child.name.startswith("."):
                        continue
                    child_metadata = child.stat()
                    if not stat.S_ISDIR(child_metadata.st_mode):
                        continue
                    child_directories.append(child)
                    if len(child_directories) > self.maximum_direct_child_directories:
                        return _unavailable_discovery(
                            policy,
                            SkillCatalogUnavailableReason.DISCOVERY_OVERBOUND,
                            "skill_direct_child_bound_exceeded",
                            diagnostics,
                        )
                for child in sorted(child_directories, key=lambda item: item.name):
                    _check_discovery_deadline(deadline_monotonic)
                    if not _is_within(child, root.containment_root):
                        return _unavailable_discovery(
                            policy,
                            SkillCatalogUnavailableReason.DISCOVERY_RACED,
                            "skill_directory_escape",
                            diagnostics,
                        )
                    skill_file = child / SKILL_FILE_NAME
                    try:
                        skill_file.lstat()
                    except FileNotFoundError:
                        continue
                    if not _is_within(skill_file, root.containment_root):
                        return _unavailable_discovery(
                            policy,
                            SkillCatalogUnavailableReason.DISCOVERY_RACED,
                            "skill_file_escape",
                            diagnostics,
                        )
                    candidates.append((root, skill_file))
        except (OSError, RuntimeError, TimeoutError):
            return _unavailable_discovery(
                policy,
                SkillCatalogUnavailableReason.DISCOVERY_RACED,
                "skill_enumeration_raced",
                diagnostics,
            )

        skills: list[LocalSkillManifest] = []
        seen_names: set[str] = set()
        observed_bytes = 0
        for root, skill_file in candidates:
            try:
                _check_discovery_deadline(deadline_monotonic)
                data = _read_bounded_bytes(
                    skill_file, maximum=self.max_skill_file_bytes
                )
            except (OSError, RuntimeError, TimeoutError):
                return _unavailable_discovery(
                    policy,
                    SkillCatalogUnavailableReason.DISCOVERY_RACED,
                    "skill_read_raced",
                    diagnostics,
                    candidate_count=len(candidates),
                    observed_bytes=observed_bytes,
                )
            observed_bytes += len(data)
            if observed_bytes > self.maximum_discovery_skill_bytes:
                return _unavailable_discovery(
                    policy,
                    SkillCatalogUnavailableReason.DISCOVERY_OVERBOUND,
                    "skill_discovery_byte_bound_exceeded",
                    diagnostics,
                    candidate_count=len(candidates),
                    observed_bytes=observed_bytes,
                )
            manifest, item_diagnostics = _parse_skill_document(
                data,
                path=skill_file,
                root=root,
                maximum_file_bytes=self.max_skill_file_bytes,
            )
            diagnostics.extend(item_diagnostics)
            if manifest is None:
                continue
            if manifest.name in seen_names:
                diagnostics.append(
                    _diagnostic(
                        SkillDiagnosticSeverity.WARNING,
                        "skill_duplicate_name",
                        "Duplicate Skill name ignored by deterministic precedence",
                        path=skill_file,
                    )
                )
                continue
            seen_names.add(manifest.name)
            skills.append(manifest)
            if len(skills) > self.maximum_admitted_skills:
                return _unavailable_discovery(
                    policy,
                    SkillCatalogUnavailableReason.CATALOG_OVERBOUND,
                    "skill_winner_bound_exceeded",
                    diagnostics,
                    candidate_count=len(candidates),
                    observed_bytes=observed_bytes,
                )
        try:
            _check_discovery_deadline(deadline_monotonic)
        except TimeoutError:
            return _unavailable_discovery(
                policy,
                SkillCatalogUnavailableReason.DISCOVERY_RACED,
                "skill_discovery_deadline_expired",
                diagnostics,
                candidate_count=len(candidates),
                observed_bytes=observed_bytes,
            )
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
        except SkillProjectionOverbound:
            return _unavailable_discovery(
                policy,
                SkillCatalogUnavailableReason.CATALOG_OVERBOUND,
                "skill_catalog_projection_bound_exceeded",
                diagnostics,
                candidate_count=len(candidates),
                observed_bytes=observed_bytes,
            )
        return LocalSkillDiscovery(
            skills=tuple(skills),
            diagnostics=_bounded_diagnostics(diagnostics),
            root_policy=policy,
            disposition=SkillDiscoveryDisposition.COMPLETE,
            unavailable_reason=None,
            enumerated_candidate_count=len(candidates),
            observed_utf8_bytes=observed_bytes,
        )

    def _prepare_root_binding(
        self, workspace_root: Path, root_kind: LocalSkillRootKind
    ) -> PreparedSkillRootBinding:
        if root_kind is LocalSkillRootKind.WORKSPACE_PULSARA:
            path = workspace_root.joinpath(*WORKSPACE_PRODUCT_SKILL_ROOT_PARTS)
            containment = workspace_root
        elif root_kind is LocalSkillRootKind.WORKSPACE_AGENTS:
            path = workspace_root.joinpath(*WORKSPACE_AGENTS_SKILL_ROOT_PARTS)
            containment = workspace_root
        elif root_kind is LocalSkillRootKind.USER_PULSARA:
            path = self.user_product_skills_root or _default_user_product_skills_root()
            path = path.expanduser().resolve()
            containment = path
        elif root_kind is LocalSkillRootKind.USER_AGENTS:
            path = self.user_agents_skills_root or Path.home().joinpath(
                *USER_AGENTS_SKILL_ROOT_PARTS
            )
            path = path.expanduser().resolve()
            containment = path
        else:  # pragma: no cover - the StrEnum is closed
            raise TypeError("unknown Skill root kind")
        path = path.expanduser().resolve()
        containment = containment.expanduser().resolve()
        ordinal = _ROOT_ORDER.index(root_kind)
        prefix = _ROOT_LOCATION_PREFIX[root_kind]
        return PreparedSkillRootBinding(
            root_kind=root_kind,
            path=path,
            containment_root=containment,
            location_prefix=prefix,
            precedence_ordinal=ordinal,
            _owner_authority=self._owner_authority,
            _constructor=_ROOT_POLICY_CONSTRUCTOR,
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


def _parse_skill_document(
    data: bytes,
    *,
    path: Path,
    root: PreparedSkillRootBinding,
    maximum_file_bytes: int,
) -> tuple[LocalSkillManifest | None, tuple[SkillDiagnostic, ...]]:
    diagnostics: list[SkillDiagnostic] = []
    if len(data) > maximum_file_bytes:
        return None, (
            _diagnostic(
                SkillDiagnosticSeverity.WARNING,
                "skill_document_overbound",
                "Skill document exceeds the 64 KiB parser bound",
                path=path,
            ),
        )
    try:
        document = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, (
            _diagnostic(
                SkillDiagnosticSeverity.ERROR,
                "skill_invalid_utf8",
                "Skill document is not valid UTF-8",
                path=path,
            ),
        )
    frontmatter, body = _extract_frontmatter(document)
    if frontmatter is None or body is None:
        return None, (
            _diagnostic(
                SkillDiagnosticSeverity.WARNING,
                "skill_missing_frontmatter",
                "Skill document has no closed YAML frontmatter",
                path=path,
            ),
        )
    if len(frontmatter.encode("utf-8")) > MAX_SKILL_FRONTMATTER_BYTES:
        return None, (
            _diagnostic(
                SkillDiagnosticSeverity.WARNING,
                "skill_frontmatter_overbound",
                "Skill frontmatter exceeds its physical bound",
                path=path,
            ),
        )
    try:
        _validate_yaml_shape(frontmatter)
        raw_fields = _load_unique_yaml_mapping(frontmatter)
    except (ValueError, yaml.YAMLError, _DuplicateYamlKey) as exc:
        return None, (
            _diagnostic(
                SkillDiagnosticSeverity.WARNING,
                "skill_invalid_frontmatter_yaml",
                f"Skill frontmatter is invalid: {type(exc).__name__}",
                path=path,
            ),
        )
    unsupported = tuple(sorted(set(raw_fields) - _STANDARD_FIELDS))
    for key in unsupported[:MAX_INTERNAL_DIAGNOSTICS]:
        code = (
            "skill_host_extension_ignored"
            if key in _HOST_EXTENSION_FIELDS
            else "skill_unknown_extension_ignored"
        )
        diagnostics.append(
            _diagnostic(
                SkillDiagnosticSeverity.INFO,
                code,
                "Unsupported Skill frontmatter field is behaviorally inert",
                path=path,
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
            _diagnostic(
                SkillDiagnosticSeverity.WARNING,
                "skill_invalid_name",
                "Skill name does not satisfy the Agent Skills core contract",
                path=path,
            )
        )
        invalid = True
    elif name != path.parent.name:
        diagnostics.append(
            _diagnostic(
                SkillDiagnosticSeverity.WARNING,
                "skill_directory_name_mismatch",
                "Skill name must exactly equal its parent directory name",
                path=path,
            )
        )
        invalid = True
    if (
        description is None
        or not 1 <= len(description) <= MAX_SKILL_DESCRIPTION_CHARS
    ):
        diagnostics.append(
            _diagnostic(
                SkillDiagnosticSeverity.WARNING,
                "skill_invalid_description",
                "Skill description is outside the Agent Skills core bound",
                path=path,
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
                SkillDiagnosticSeverity.WARNING,
                "skill_invalid_license",
                "Skill license is outside its closed string bound",
                path=path,
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
                "skill_invalid_compatibility",
                "Skill compatibility is outside its closed character bound",
                path=path,
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
                SkillDiagnosticSeverity.WARNING,
                "skill_invalid_metadata",
                "Skill metadata must be a bounded string-to-string mapping",
                path=path,
            )
        )
        invalid = True
    location = _skill_location(path, root=root)
    if len(location.encode("utf-8")) > MAX_SKILL_LOCATION_BYTES:
        diagnostics.append(
            _diagnostic(
                SkillDiagnosticSeverity.WARNING,
                "skill_location_overbound",
                "Skill model-visible location exceeds its bound",
                path=path,
            )
        )
        invalid = True
    if invalid or name is None or description is None:
        return None, _bounded_diagnostics(diagnostics)

    authoring: list[SkillAuthoringDiagnosticCode] = []
    if len(body.splitlines()) > 500:
        authoring.append(SkillAuthoringDiagnosticCode.BODY_OVER_500_LINES)
        diagnostics.append(
            _diagnostic(
                SkillDiagnosticSeverity.INFO,
                SkillAuthoringDiagnosticCode.BODY_OVER_500_LINES.value,
                "Skill body exceeds the portable authoring recommendation",
                path=path,
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
    return (
        LocalSkillManifest(
            name=name,
            description=description,
            license=license_value,
            compatibility=compatibility,
            metadata=metadata,
            path=path,
            base_dir=path.parent,
            location=location,
            body=body,
            raw_document_digest=raw_digest,
            manifest_semantic_fingerprint=semantic,
            root_kind=root.root_kind,
            authoring_diagnostic_codes=tuple(authoring),
            raw_document=document,
        ),
        _bounded_diagnostics(diagnostics),
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


def _read_bounded_bytes(path: Path, *, maximum: int) -> bytes:
    """Read one lexical ``root/skill/SKILL.md`` candidate without path races.

    Discovery is planning-deadline bounded.  In particular, a local FIFO must
    not turn the supposedly bounded scan into an unbounded blocking open.  The
    root, child directory, and final regular file are therefore opened as one
    descriptor-relative, no-follow chain; the opened file identity must remain
    stable for the duration of the bounded read.
    """

    root = path.parent.parent
    if path.name != SKILL_FILE_NAME or path.parent == root:
        raise OSError("invalid Skill candidate shape")
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY
    file_flags |= getattr(os, "O_CLOEXEC", 0)
    file_flags |= getattr(os, "O_NONBLOCK", 0)
    file_flags |= getattr(os, "O_NOFOLLOW", 0)

    root_fd = os.open(root, directory_flags)
    try:
        child_fd = os.open(path.parent.name, directory_flags, dir_fd=root_fd)
        try:
            file_fd = os.open(path.name, file_flags, dir_fd=child_fd)
            try:
                before = os.fstat(file_fd)
                if not stat.S_ISREG(before.st_mode):
                    raise OSError("Skill candidate is not a regular file")
                remaining = maximum + 1
                chunks: list[bytes] = []
                while remaining:
                    chunk = os.read(file_fd, remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                after = os.fstat(file_fd)
                identity_before = (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
                identity_after = (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
                if identity_after != identity_before:
                    raise OSError("Skill candidate changed during read")
                return b"".join(chunks)
            finally:
                os.close(file_fd)
        finally:
            os.close(child_fd)
    finally:
        os.close(root_fd)


def _skill_location(path: Path, *, root: PreparedSkillRootBinding) -> str:
    if path.name != SKILL_FILE_NAME or path.parent.parent != root.path:
        raise ValueError("Skill candidate does not match its frozen lexical root")
    relative = path.relative_to(root.path).as_posix()
    return f"{root.location_prefix}/{relative}"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return False
    return True


def _default_user_product_skills_root() -> Path:
    pulsara_home = os.getenv(PULSARA_HOME_ENV)
    if pulsara_home:
        return Path(pulsara_home).expanduser().resolve() / "skills"
    return Path.home().joinpath(*USER_PRODUCT_SKILL_ROOT_PARTS).resolve()


def _check_discovery_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
        raise TimeoutError("local Skill discovery deadline expired")


def _validate_scope(
    scope: ModelInputScopeKind, scope_subagent_task_id: str | None
) -> None:
    if not isinstance(scope, ModelInputScopeKind):
        raise TypeError("Skill root policy scope is not closed")
    if (scope is ModelInputScopeKind.ROOT) != (scope_subagent_task_id is None):
        raise ValueError("Skill root policy scope identity is invalid")


def _root_binding_fingerprint(
    *,
    root_kind: LocalSkillRootKind,
    path: Path,
    containment_root: Path,
    location_prefix: str,
    precedence_ordinal: int,
) -> str:
    return context_fingerprint(
        "local-skill-root-binding:v1",
        {
            "root_kind": root_kind.value,
            "path": str(path),
            "containment_root": str(containment_root),
            "location_prefix": location_prefix,
            "precedence_ordinal": precedence_ordinal,
        },
    )


def local_skill_root_policy_identity_digest(
    policy: PreparedLocalSkillRootPolicy,
) -> str:
    """Derive catalog lineage without duplicating the owner-issued policy."""

    return context_fingerprint(
        "local-skill-root-policy:v2-agent-skills",
        {
            "scope": policy.conversation_scope_kind.value,
            "scope_subagent_task_id": policy.scope_subagent_task_id,
            "roots": tuple(
                _root_binding_fingerprint(
                    root_kind=item.root_kind,
                    path=item.path,
                    containment_root=item.containment_root,
                    location_prefix=item.location_prefix,
                    precedence_ordinal=item.precedence_ordinal,
                )
                for item in policy.roots
            ),
        },
    )


def _diagnostic(
    severity: SkillDiagnosticSeverity,
    code: str,
    message: str,
    *,
    path: Path | None = None,
) -> SkillDiagnostic:
    return SkillDiagnostic(severity=severity, code=code, message=message, path=path)


def _bounded_diagnostics(
    values: list[SkillDiagnostic] | tuple[SkillDiagnostic, ...],
) -> tuple[SkillDiagnostic, ...]:
    return tuple(values[:MAX_INTERNAL_DIAGNOSTICS])


def _unavailable_discovery(
    policy: PreparedLocalSkillRootPolicy,
    reason: SkillCatalogUnavailableReason,
    code: str,
    diagnostics: list[SkillDiagnostic],
    *,
    candidate_count: int = 0,
    observed_bytes: int = 0,
) -> LocalSkillDiscovery:
    return LocalSkillDiscovery(
        skills=(),
        diagnostics=_bounded_diagnostics(
            [
                *diagnostics,
                _diagnostic(
                    SkillDiagnosticSeverity.ERROR,
                    code,
                    "The complete local Skill catalog could not be proven",
                ),
            ]
        ),
        root_policy=policy,
        disposition=SkillDiscoveryDisposition.UNAVAILABLE,
        unavailable_reason=reason,
        enumerated_candidate_count=candidate_count,
        observed_utf8_bytes=observed_bytes,
    )


__all__ = [
    "AGENT_SKILLS_CONTRACT_ID",
    "BUNDLED_SKILL_PROVENANCE_FILE_NAME",
    "LocalSkillDiscovery",
    "LocalSkillProvider",
    "PreparedLocalSkillRootPolicy",
    "PreparedSkillRootBinding",
    "SkillDiscoveryDisposition",
    "local_skill_root_policy_identity_digest",
    "local_skill_manifest_semantic_fingerprint",
]
