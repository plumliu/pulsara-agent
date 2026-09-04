"""Strict Agent Plugins 1.0 parsing over one held no-follow observation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import stat
from time import monotonic
from typing import Mapping
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from pulsara_agent.capability.local_skills import (
    MAX_SKILL_FILE_BYTES,
    ParsedSkillDocument,
    diagnostic_at,
    parse_skill_document,
    validate_skill_candidate_placement,
)
from pulsara_agent.capability.types import (
    SkillDiagnostic,
    SkillDiagnosticCode,
    SkillDiagnosticSeverity,
)
from pulsara_agent.hooks.config_parser import (
    HookConfigParseError,
    MAXIMUM_HOOK_CONFIG_BYTES,
    ParsedHookConfig,
    parse_hook_config,
)
from pulsara_agent.hooks.contracts import (
    FrozenHookSourceProvenance,
    HookDiagnostic,
    HookVisibilityScope,
    PluginHookSourceIdentity,
    PluginHookTrustSubject,
)
from pulsara_agent.local_source_binding import (
    DIRECTORY_NOFOLLOW_FLAGS,
    open_absolute_directory_nofollow,
    prepare_local_source_path,
)
from pulsara_agent.plugins.contracts import (
    PLUGIN_MANIFEST_SCHEMA_ID,
    PLUGIN_MCP_SCHEMA_ID,
    NeverCancelPluginOperation,
    PluginAuthor,
    PluginCancellationPort,
    PluginComponentObservationDisposition,
    PluginComponentSummary,
    PluginDiagnostic,
    PluginDiagnosticCode,
    PluginHookDefinitionSummary,
    PluginManifest,
    PluginMcpHttpSummary,
    PluginMcpServerSummary,
    PluginMcpStdioSummary,
    PluginSkillSummary,
    PluginValidationSummary,
)
from pulsara_agent.primitives.bounded_json import (
    DuplicateJsonKey,
    JsonBoundExceeded,
    bounded_json_loads,
)
from pulsara_agent.process_credential_boundary import (
    ProcessCredentialBoundary,
    ProcessCredentialBoundaryCancelled,
    ProcessCredentialBoundaryTimedOut,
    ProcessCredentialScrubSet,
)


MAXIMUM_PLUGIN_JSON_BYTES = 1024 * 1024
MAXIMUM_PLUGIN_JSON_NODES = 16_384
MAXIMUM_PLUGIN_JSON_DEPTH = 64
MAXIMUM_PLUGIN_JSON_SCALAR_BYTES = 64 * 1024
COPY_CHUNK_BYTES = 64 * 1024
_VALIDATION_INSTALL_ID = "pkg_00000000000000000000000000000000"
_TCHAR = frozenset(
    b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
_MANIFEST_FIELDS = frozenset(
    {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "extensions",
    }
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class PackageEntryKind(StrEnum):
    DIRECTORY = "DIRECTORY"
    REGULAR_FILE = "REGULAR_FILE"
    SYMLINK = "SYMLINK"
    SPECIAL = "SPECIAL"


@dataclass(frozen=True, slots=True)
class FrozenPackageEntry:
    relative_path: PurePosixPath
    kind: PackageEntryKind
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    executable: bool


@dataclass(frozen=True, slots=True)
class ParsedPluginSkillCandidate:
    relative_directory: PurePosixPath
    document_path: Path
    parsed: ParsedSkillDocument
    diagnostics: tuple[SkillDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class ParsedPluginMcpComponent:
    declared_server_ids: tuple[str, ...]
    valid_servers: tuple[PluginMcpServerSummary, ...]
    diagnostics: tuple[PluginDiagnostic, ...]


@dataclass(slots=True)
class HeldPluginPackageObservation:
    source_path: Path
    descriptor: int
    root_device: int
    root_inode: int
    entries: tuple[FrozenPackageEntry, ...]
    summary: PluginValidationSummary
    diagnostics: tuple[object, ...]
    skill_candidates: tuple[ParsedPluginSkillCandidate, ...]
    skill_invalid_diagnostics: tuple[SkillDiagnostic, ...]
    mcp_component: ParsedPluginMcpComponent | None
    hook_config: ParsedHookConfig | None
    enforce_managed_admission: bool = True
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self.descriptor)

    def __enter__(self) -> "HeldPluginPackageObservation":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class PluginPackageInvalid(ValueError):
    def __init__(self, diagnostics: tuple[PluginDiagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("invalid package requires diagnostics")
        self.diagnostics = diagnostics
        super().__init__(diagnostics[0].code.value)


class PluginPackageUnavailable(OSError):
    def __init__(self, code: PluginDiagnosticCode) -> None:
        self.code = code
        super().__init__(code.value)


class PluginPackageRaced(OSError):
    pass


class PluginPackageCancelled(RuntimeError):
    pass


class PluginPackageTimedOut(TimeoutError):
    pass


class PluginSourceObserver:
    """One held source/root owner shared by validate, install, and Runtime."""

    def __init__(self, credential_boundary: ProcessCredentialBoundary) -> None:
        self._credential_boundary = credential_boundary

    def observe(
        self,
        source_path: Path,
        *,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort | None = None,
        scrub_set: ProcessCredentialScrubSet | None = None,
        hook_package_install_id: str = _VALIDATION_INSTALL_ID,
        hook_visibility: HookVisibilityScope = HookVisibilityScope.USER,
        hook_workspace_state_key: str | None = None,
        hook_lifetime_anchor: object | None = None,
        hook_declaration_environment: tuple[tuple[str, str], ...] = (),
        scan_active_api_key: bool = True,
        enforce_managed_admission: bool = True,
    ) -> HeldPluginPackageObservation:
        source = prepare_local_source_path(source_path)
        probe = cancellation or NeverCancelPluginOperation()
        scrub = scrub_set or ProcessCredentialScrubSet()
        _check_abort(deadline_monotonic, probe)
        descriptor = _open_source_root(source)
        try:
            try:
                root = os.fstat(descriptor)
                entries = _observe_membership(
                    descriptor,
                    deadline_monotonic=deadline_monotonic,
                    cancellation=probe,
                    enforce_managed_admission=enforce_managed_admission,
                )
            except (PluginPackageCancelled, PluginPackageTimedOut):
                raise
            except PluginPackageInvalid:
                raise
            except MemoryError as exc:
                raise PluginPackageUnavailable(
                    PluginDiagnosticCode.SOURCE_UNAVAILABLE
                ) from exc
            except OSError as exc:
                raise PluginPackageUnavailable(
                    PluginDiagnosticCode.SOURCE_UNAVAILABLE
                ) from exc
            by_path = {entry.relative_path: entry for entry in entries}
            manifest_entry = by_path.get(PurePosixPath("plugin.json"))
            if (
                manifest_entry is None
                or manifest_entry.kind is not PackageEntryKind.REGULAR_FILE
            ):
                raise PluginPackageInvalid(
                    (
                        _diagnostic(
                            PluginDiagnosticCode.MANIFEST_MISSING,
                            source / "plugin.json",
                        ),
                    )
                )
            try:
                manifest_bytes = _read_bounded_entry(
                    descriptor,
                    manifest_entry,
                    maximum=MAXIMUM_PLUGIN_JSON_BYTES,
                    deadline_monotonic=deadline_monotonic,
                    cancellation=probe,
                )
            except JsonBoundExceeded as exc:
                raise PluginPackageInvalid(
                    (
                        _diagnostic(
                            PluginDiagnosticCode.MANIFEST_OVERBOUND,
                            source / "plugin.json",
                        ),
                    )
                ) from exc
            manifest, manifest_diagnostics = _parse_manifest(
                manifest_bytes, source / "plugin.json"
            )
            try:
                skills_summary, skill_candidates, skill_invalid = _parse_skills(
                    descriptor,
                    entries,
                    source=source,
                    deadline_monotonic=deadline_monotonic,
                    cancellation=probe,
                )
            except (PluginPackageRaced, PluginPackageUnavailable, MemoryError):
                skills_summary = _component_unavailable(source / "skills")
                skill_candidates, skill_invalid = (), ()
            try:
                mcp_summary, mcp_component = _parse_mcp(
                    descriptor,
                    entries,
                    source=source,
                    deadline_monotonic=deadline_monotonic,
                    cancellation=probe,
                )
            except (PluginPackageRaced, PluginPackageUnavailable, MemoryError):
                mcp_summary = _component_unavailable(source / "mcp.json")
                mcp_component = None
            try:
                hooks_summary, hook_config = _parse_hooks(
                    descriptor,
                    entries,
                    source=source,
                    manifest=manifest,
                    package_install_id=hook_package_install_id,
                    visibility=hook_visibility,
                    workspace_state_key=hook_workspace_state_key,
                    lifetime_anchor=hook_lifetime_anchor,
                    declaration_environment=hook_declaration_environment,
                    deadline_monotonic=deadline_monotonic,
                    cancellation=probe,
                )
            except (PluginPackageRaced, PluginPackageUnavailable, MemoryError):
                hooks_summary = _component_unavailable(
                    source / "dev.pulsara/hooks/hooks.json"
                )
                hook_config = None
            if scan_active_api_key:
                _stable_secret_scan(
                    self._credential_boundary,
                    descriptor,
                    entries,
                    source_path=os.fsencode(source),
                    deadline_monotonic=deadline_monotonic,
                    cancellation=probe,
                    scrub_set=scrub,
                )
            _revalidate_source(
                source,
                descriptor,
                root=(root.st_dev, root.st_ino),
                expected_entries=entries,
                deadline_monotonic=deadline_monotonic,
                cancellation=probe,
                enforce_managed_admission=enforce_managed_admission,
            )
            summary = PluginValidationSummary(
                manifest=manifest,
                skills=skills_summary,
                mcp=mcp_summary,
                hooks=hooks_summary,
            )
            diagnostics: tuple[object, ...] = (
                *manifest_diagnostics,
                *skills_summary.diagnostics,
                *mcp_summary.diagnostics,
                *hooks_summary.diagnostics,
            )
            return HeldPluginPackageObservation(
                source,
                descriptor,
                root.st_dev,
                root.st_ino,
                entries,
                summary,
                diagnostics,
                skill_candidates,
                skill_invalid,
                mcp_component,
                hook_config,
                enforce_managed_admission,
            )
        except BaseException:
            os.close(descriptor)
            raise


def _open_source_root(source: Path) -> int:
    try:
        return open_absolute_directory_nofollow(source)
    except FileNotFoundError as exc:
        raise PluginPackageInvalid(
            (_diagnostic(PluginDiagnosticCode.SOURCE_NOT_DIRECTORY, source),)
        ) from exc
    except OSError as exc:
        try:
            metadata = source.lstat()
        except OSError:
            raise PluginPackageUnavailable(
                PluginDiagnosticCode.SOURCE_UNAVAILABLE
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PluginPackageInvalid(
                (_diagnostic(PluginDiagnosticCode.SOURCE_FINAL_SYMLINK, source),)
            ) from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise PluginPackageInvalid(
                (_diagnostic(PluginDiagnosticCode.SOURCE_NOT_DIRECTORY, source),)
            ) from exc
        raise PluginPackageUnavailable(PluginDiagnosticCode.SOURCE_UNAVAILABLE) from exc


def _component_unavailable(path: Path) -> PluginComponentSummary:
    return PluginComponentSummary(
        PluginComponentObservationDisposition.UNAVAILABLE,
        diagnostics=(_diagnostic(PluginDiagnosticCode.SOURCE_UNAVAILABLE, path),),
    )


def _observe_membership(
    root_fd: int,
    *,
    deadline_monotonic: float,
    cancellation: PluginCancellationPort,
    enforce_managed_admission: bool = True,
) -> tuple[FrozenPackageEntry, ...]:
    result: list[FrozenPackageEntry] = []
    stack: list[tuple[int, PurePosixPath]] = [(os.dup(root_fd), PurePosixPath())]
    try:
        while stack:
            directory_fd, prefix = stack.pop()
            try:
                _check_abort(deadline_monotonic, cancellation)
                try:
                    names = tuple(sorted(os.listdir(directory_fd)))
                except OSError as exc:
                    raise PluginPackageUnavailable(
                        PluginDiagnosticCode.SOURCE_UNAVAILABLE
                    ) from exc
                children: list[tuple[int, PurePosixPath]] = []
                for name in names:
                    _check_abort(deadline_monotonic, cancellation)
                    if name in {"", ".", ".."} or "/" in name or "\x00" in name:
                        raise PluginPackageInvalid(
                            (_diagnostic(PluginDiagnosticCode.SOURCE_ESCAPE),)
                        )
                    relative = prefix / name if prefix.parts else PurePosixPath(name)
                    try:
                        metadata = os.stat(
                            name, dir_fd=directory_fd, follow_symlinks=False
                        )
                    except FileNotFoundError as exc:
                        raise PluginPackageRaced from exc
                    except OSError as exc:
                        raise PluginPackageUnavailable(
                            PluginDiagnosticCode.SOURCE_UNAVAILABLE
                        ) from exc
                    if stat.S_ISLNK(metadata.st_mode):
                        if enforce_managed_admission:
                            raise PluginPackageInvalid(
                                (_diagnostic(PluginDiagnosticCode.SOURCE_TREE_SYMLINK),)
                            )
                        kind = PackageEntryKind.SYMLINK
                    elif stat.S_ISDIR(metadata.st_mode):
                        kind = PackageEntryKind.DIRECTORY
                    elif stat.S_ISREG(metadata.st_mode):
                        kind = PackageEntryKind.REGULAR_FILE
                    else:
                        if enforce_managed_admission:
                            raise PluginPackageInvalid(
                                (_diagnostic(PluginDiagnosticCode.SOURCE_SPECIAL_FILE),)
                            )
                        kind = PackageEntryKind.SPECIAL
                    entry = _freeze_entry(relative, kind, metadata)
                    result.append(entry)
                    if kind is PackageEntryKind.DIRECTORY:
                        try:
                            child = os.open(
                                name, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=directory_fd
                            )
                            opened = os.fstat(child)
                        except FileNotFoundError as exc:
                            raise PluginPackageRaced from exc
                        except OSError as exc:
                            raise PluginPackageUnavailable(
                                PluginDiagnosticCode.SOURCE_UNAVAILABLE
                            ) from exc
                        if not _entry_matches(entry, opened):
                            os.close(child)
                            raise PluginPackageRaced
                        children.append((child, relative))
                stack.extend(reversed(children))
            finally:
                os.close(directory_fd)
    except BaseException:
        for descriptor, _prefix in stack:
            os.close(descriptor)
        raise
    return tuple(sorted(result, key=lambda item: item.relative_path.as_posix()))


def _freeze_entry(
    relative: PurePosixPath, kind: PackageEntryKind, metadata: os.stat_result
) -> FrozenPackageEntry:
    return FrozenPackageEntry(
        relative,
        kind,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        bool(metadata.st_mode & 0o111)
        if kind is PackageEntryKind.REGULAR_FILE
        else False,
    )


def _entry_matches(entry: FrozenPackageEntry, metadata: os.stat_result) -> bool:
    if entry.kind is PackageEntryKind.DIRECTORY:
        expected_kind = stat.S_ISDIR(metadata.st_mode)
    elif entry.kind is PackageEntryKind.REGULAR_FILE:
        expected_kind = stat.S_ISREG(metadata.st_mode)
    elif entry.kind is PackageEntryKind.SYMLINK:
        expected_kind = stat.S_ISLNK(metadata.st_mode)
    elif entry.kind is PackageEntryKind.SPECIAL:
        expected_kind = not (
            stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
        )
    else:  # pragma: no cover - closed entry-kind union
        raise TypeError("Plugin package entry kind is open")
    return expected_kind and (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        bool(metadata.st_mode & 0o111)
        if entry.kind is PackageEntryKind.REGULAR_FILE
        else False,
    ) == (
        entry.device,
        entry.inode,
        entry.size,
        entry.mtime_ns,
        entry.ctime_ns,
        entry.executable,
    )


def _read_bounded_entry(
    root_fd: int,
    entry: FrozenPackageEntry,
    *,
    maximum: int,
    deadline_monotonic: float,
    cancellation: PluginCancellationPort,
) -> bytes:
    descriptor = _open_regular_entry(root_fd, entry)
    try:
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            _check_abort(deadline_monotonic, cancellation)
            try:
                chunk = os.read(descriptor, min(COPY_CHUNK_BYTES, remaining))
            except OSError as exc:
                raise PluginPackageUnavailable(
                    PluginDiagnosticCode.SOURCE_UNAVAILABLE
                ) from exc
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if not _entry_matches(entry, os.fstat(descriptor)):
            raise PluginPackageRaced
        value = b"".join(chunks)
        if len(value) > maximum:
            raise JsonBoundExceeded
        return value
    finally:
        os.close(descriptor)


def _open_regular_entry(root_fd: int, entry: FrozenPackageEntry) -> int:
    if entry.kind is not PackageEntryKind.REGULAR_FILE:
        raise ValueError("regular package entry required")
    parent, name = _open_relative_parent(root_fd, entry.relative_path)
    try:
        try:
            descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent)
        except FileNotFoundError as exc:
            raise PluginPackageRaced from exc
        except OSError as exc:
            raise PluginPackageUnavailable(
                PluginDiagnosticCode.SOURCE_UNAVAILABLE
            ) from exc
        if not _entry_matches(entry, os.fstat(descriptor)):
            os.close(descriptor)
            raise PluginPackageRaced
        return descriptor
    finally:
        os.close(parent)


def _open_relative_parent(root_fd: int, relative: PurePosixPath) -> tuple[int, str]:
    parts = relative.parts
    if not parts or any(item in {"", ".", ".."} for item in parts):
        raise ValueError("package relative path is invalid")
    current = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=current)
            os.close(current)
            current = next_fd
        return current, parts[-1]
    except BaseException:
        os.close(current)
        raise


def _parse_manifest(
    raw: bytes, path: Path
) -> tuple[PluginManifest, tuple[PluginDiagnostic, ...]]:
    try:
        value = bounded_json_loads(
            raw,
            maximum_bytes=MAXIMUM_PLUGIN_JSON_BYTES,
            maximum_nodes=MAXIMUM_PLUGIN_JSON_NODES,
            maximum_depth=MAXIMUM_PLUGIN_JSON_DEPTH,
            maximum_string_utf8_bytes=MAXIMUM_PLUGIN_JSON_SCALAR_BYTES,
            reject_duplicate_keys=True,
        )
    except UnicodeDecodeError as exc:  # pragma: no cover - json decoder wrapper
        raise PluginPackageInvalid(
            (_diagnostic(PluginDiagnosticCode.MANIFEST_INVALID_UTF8, path),)
        ) from exc
    except JsonBoundExceeded as exc:
        raise PluginPackageInvalid(
            (_diagnostic(PluginDiagnosticCode.MANIFEST_OVERBOUND, path),)
        ) from exc
    except (DuplicateJsonKey, ValueError) as exc:
        code = (
            PluginDiagnosticCode.MANIFEST_INVALID_UTF8
            if not _is_utf8(raw)
            else PluginDiagnosticCode.MANIFEST_INVALID_JSON
        )
        raise PluginPackageInvalid((_diagnostic(code, path),)) from exc
    if not isinstance(value, dict):
        raise PluginPackageInvalid(
            (_diagnostic(PluginDiagnosticCode.MANIFEST_INVALID, path),)
        )
    schema_value = value.get("$schema")
    if schema_value != PLUGIN_MANIFEST_SCHEMA_ID:
        raise PluginPackageInvalid(
            (_diagnostic(PluginDiagnosticCode.MANIFEST_SCHEMA_UNSUPPORTED, path),)
        )
    diagnostics: list[PluginDiagnostic] = []
    normative = dict(value)
    for key in sorted(set(normative) - _MANIFEST_FIELDS):
        diagnostics.append(
            _diagnostic(
                PluginDiagnosticCode.MANIFEST_UNKNOWN_FIELD_IGNORED,
                path,
                component=key,
            )
        )
        normative.pop(key)
    extension_names: tuple[str, ...] = ()
    extensions = normative.get("extensions")
    if extensions is not None:
        diagnostics.append(
            _diagnostic(PluginDiagnosticCode.EXTENSIONS_FIELD_IGNORED, path)
        )
        if isinstance(extensions, dict):
            extension_names = tuple(sorted(extensions))
            normative["extensions"] = {}
        else:
            normative.pop("extensions")
    validator = _schema_validator("plugin.schema.json", PLUGIN_MANIFEST_SCHEMA_ID)
    if next(validator.iter_errors(normative), None) is not None:
        raise PluginPackageInvalid(
            (_diagnostic(PluginDiagnosticCode.MANIFEST_INVALID, path),)
        )
    author_value = normative.get("author")
    author = (
        PluginAuthor(
            author_value.get("name"),
            author_value.get("email"),
            author_value.get("url"),
        )
        if isinstance(author_value, dict)
        else None
    )
    manifest = PluginManifest(
        schema=PLUGIN_MANIFEST_SCHEMA_ID,
        name=normative["name"],
        version=normative.get("version"),
        description=normative.get("description"),
        author=author,
        homepage=normative.get("homepage"),
        repository=normative.get("repository"),
        license=normative.get("license"),
        keywords=tuple(normative.get("keywords", ())),
        extension_names=extension_names,
    )
    return manifest, tuple(diagnostics)


def _parse_skills(
    root_fd: int,
    entries: tuple[FrozenPackageEntry, ...],
    *,
    source: Path,
    deadline_monotonic: float,
    cancellation: PluginCancellationPort,
) -> tuple[
    PluginComponentSummary,
    tuple[ParsedPluginSkillCandidate, ...],
    tuple[SkillDiagnostic, ...],
]:
    by_path = {item.relative_path: item for item in entries}
    root = by_path.get(PurePosixPath("skills"))
    if root is None:
        return (
            PluginComponentSummary(PluginComponentObservationDisposition.MISSING),
            (),
            (),
        )
    if root.kind is not PackageEntryKind.DIRECTORY:
        diagnostic = _diagnostic(
            PluginDiagnosticCode.COMPONENT_KIND_INVALID, source / "skills"
        )
        return (
            PluginComponentSummary(
                PluginComponentObservationDisposition.INVALID,
                diagnostics=(diagnostic,),
            ),
            (),
            (),
        )
    direct_children = tuple(
        item
        for item in entries
        if len(item.relative_path.parts) == 2
        and item.relative_path.parts[0] == "skills"
    )
    candidates: list[ParsedPluginSkillCandidate] = []
    invalid: list[SkillDiagnostic] = []
    summaries: list[PluginSkillSummary] = []
    for child in direct_children:
        _check_abort(deadline_monotonic, cancellation)
        if child.kind is not PackageEntryKind.DIRECTORY:
            continue
        document_relative = child.relative_path / "SKILL.md"
        document_entry = by_path.get(document_relative)
        document_path = source.joinpath(*document_relative.parts)
        if (
            document_entry is None
            or document_entry.kind is not PackageEntryKind.REGULAR_FILE
        ):
            code = (
                SkillDiagnosticCode.FILE_ESCAPE
                if document_entry is not None
                and document_entry.kind is PackageEntryKind.SYMLINK
                else SkillDiagnosticCode.MISSING_DOCUMENT
            )
            invalid.append(
                SkillDiagnostic(
                    SkillDiagnosticSeverity.WARNING,
                    code,
                    (
                        "Skill source SKILL.md escapes its package directory"
                        if code is SkillDiagnosticCode.FILE_ESCAPE
                        else "Skill source is missing SKILL.md"
                    ),
                    document_path,
                )
            )
            continue
        try:
            raw = _read_bounded_entry(
                root_fd,
                document_entry,
                maximum=MAX_SKILL_FILE_BYTES,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            )
            parsed_result = parse_skill_document(raw)
        except JsonBoundExceeded:
            parsed_result = parse_skill_document(b"x" * (MAX_SKILL_FILE_BYTES + 1))
        placement = (
            validate_skill_candidate_placement(
                parsed_result.parsed, child.relative_path.name
            )
            if parsed_result.parsed is not None
            else None
        )
        diagnostics = tuple(
            diagnostic_at(item, document_path)
            for item in (
                *parsed_result.diagnostics,
                *((placement.diagnostics) if placement is not None else ()),
            )
        )
        if parsed_result.parsed is None or (
            placement is not None and not placement.valid
        ):
            invalid.extend(diagnostics)
            continue
        parsed = parsed_result.parsed
        location = document_relative.parent.as_posix()
        candidates.append(
            ParsedPluginSkillCandidate(
                document_relative.parent,
                document_path,
                parsed,
                diagnostics,
            )
        )
        summaries.append(PluginSkillSummary(parsed.name, parsed.description, location))
    ordered_candidates = tuple(sorted(candidates, key=lambda item: item.parsed.name))
    ordered_invalid = tuple(
        sorted(invalid, key=lambda item: (str(item.path or ""), item.code.value))
    )
    summary = PluginComponentSummary(
        PluginComponentObservationDisposition.COMPLETE,
        skills=tuple(sorted(summaries, key=lambda item: item.name)),
        diagnostics=ordered_invalid,
    )
    return summary, ordered_candidates, ordered_invalid


def _parse_mcp(
    root_fd: int,
    entries: tuple[FrozenPackageEntry, ...],
    *,
    source: Path,
    deadline_monotonic: float,
    cancellation: PluginCancellationPort,
) -> tuple[PluginComponentSummary, ParsedPluginMcpComponent | None]:
    entry = next(
        (item for item in entries if item.relative_path == PurePosixPath("mcp.json")),
        None,
    )
    if entry is None:
        return PluginComponentSummary(
            PluginComponentObservationDisposition.MISSING
        ), None
    if entry.kind is not PackageEntryKind.REGULAR_FILE:
        diagnostic = _diagnostic(
            PluginDiagnosticCode.COMPONENT_KIND_INVALID, source / "mcp.json"
        )
        return (
            PluginComponentSummary(
                PluginComponentObservationDisposition.INVALID,
                diagnostics=(diagnostic,),
            ),
            None,
        )
    try:
        raw = _read_bounded_entry(
            root_fd,
            entry,
            maximum=MAXIMUM_PLUGIN_JSON_BYTES,
            deadline_monotonic=deadline_monotonic,
            cancellation=cancellation,
        )
        value = bounded_json_loads(
            raw,
            maximum_bytes=MAXIMUM_PLUGIN_JSON_BYTES,
            maximum_nodes=MAXIMUM_PLUGIN_JSON_NODES,
            maximum_depth=MAXIMUM_PLUGIN_JSON_DEPTH,
            maximum_string_utf8_bytes=MAXIMUM_PLUGIN_JSON_SCALAR_BYTES,
            reject_duplicate_keys=True,
        )
    except (JsonBoundExceeded, DuplicateJsonKey, ValueError):
        diagnostic = _diagnostic(
            PluginDiagnosticCode.MCP_COMPONENT_INVALID, source / "mcp.json"
        )
        return (
            PluginComponentSummary(
                PluginComponentObservationDisposition.INVALID,
                diagnostics=(diagnostic,),
            ),
            None,
        )
    if not isinstance(value, dict) or value.get("$schema") != PLUGIN_MCP_SCHEMA_ID:
        diagnostic = _diagnostic(
            PluginDiagnosticCode.MCP_COMPONENT_INVALID, source / "mcp.json"
        )
        return (
            PluginComponentSummary(
                PluginComponentObservationDisposition.INVALID,
                diagnostics=(diagnostic,),
            ),
            None,
        )
    servers = value.get("mcpServers")
    if set(value) != {"$schema", "mcpServers"} or not isinstance(servers, dict):
        diagnostic = _diagnostic(
            PluginDiagnosticCode.MCP_COMPONENT_INVALID, source / "mcp.json"
        )
        return (
            PluginComponentSummary(
                PluginComponentObservationDisposition.INVALID,
                diagnostics=(diagnostic,),
            ),
            None,
        )
    schema = _load_schema("mcp.schema.json")
    server_schema = {"$ref": "#/$defs/server", "$defs": schema["$defs"]}
    registry = Registry().with_resource(
        PLUGIN_MCP_SCHEMA_ID, Resource.from_contents(schema)
    )
    validator = Draft202012Validator(server_schema, registry=registry)
    diagnostics: list[PluginDiagnostic] = []
    valid: list[PluginMcpServerSummary] = []
    declared = tuple(sorted(servers))
    for local_id in declared:
        _check_abort(deadline_monotonic, cancellation)
        raw_server = servers[local_id]
        if next(validator.iter_errors(raw_server), None) is not None:
            diagnostics.append(
                _diagnostic(
                    PluginDiagnosticCode.MCP_SERVER_INVALID,
                    source / "mcp.json",
                    component=local_id,
                )
            )
            continue
        assert isinstance(raw_server, dict)
        server = _normalize_portable_server(
            local_id, raw_server, entries=entries, source=source
        )
        if isinstance(server, PluginDiagnostic):
            diagnostics.append(server)
        else:
            valid.append(server)
    component = ParsedPluginMcpComponent(
        declared,
        tuple(sorted(valid, key=lambda item: item.local_server_id)),
        tuple(diagnostics),
    )
    summary = PluginComponentSummary(
        PluginComponentObservationDisposition.COMPLETE,
        mcp_servers=component.valid_servers,
        diagnostics=component.diagnostics,
    )
    return summary, component


def _normalize_portable_server(
    local_id: str,
    raw: Mapping[str, object],
    *,
    entries: tuple[FrozenPackageEntry, ...],
    source: Path,
) -> PluginMcpServerSummary | PluginDiagnostic:
    kind = raw["type"]
    if kind == "sse":
        return _diagnostic(
            PluginDiagnosticCode.MCP_TRANSPORT_UNSUPPORTED,
            source / "mcp.json",
            component=local_id,
        )
    if kind == "stdio":
        command = raw["command"]
        assert isinstance(command, str)
        if not _valid_stdio_command(command, entries):
            return _diagnostic(
                PluginDiagnosticCode.MCP_SERVER_INVALID,
                source / "mcp.json",
                component=local_id,
            )
        environment = raw.get("env", {})
        assert isinstance(environment, dict)
        if any(name in environment for name in ("PLUGIN_ROOT", "PLUGIN_DATA")):
            return _diagnostic(
                PluginDiagnosticCode.MCP_SERVER_INVALID,
                source / "mcp.json",
                component=local_id,
            )
        cwd = raw.get("cwd")
        if cwd is not None and (
            not isinstance(cwd, str) or not _valid_portable_cwd(cwd)
        ):
            return _diagnostic(
                PluginDiagnosticCode.MCP_SERVER_INVALID,
                source / "mcp.json",
                component=local_id,
            )
        args = raw.get("args", [])
        assert isinstance(args, list)
        return PluginMcpStdioSummary(
            local_id,
            command,
            tuple(args),  # type: ignore[arg-type]
            cwd,
            tuple(sorted(environment.items())),  # type: ignore[arg-type]
        )
    assert kind == "streamable-http"
    endpoint = raw["url"]
    headers = raw.get("headers", {})
    assert isinstance(endpoint, str) and isinstance(headers, dict)
    if not _valid_http_endpoint(endpoint) or not _valid_public_headers(headers):
        return _diagnostic(
            PluginDiagnosticCode.MCP_SERVER_INVALID,
            source / "mcp.json",
            component=local_id,
        )
    return PluginMcpHttpSummary(
        local_id,
        endpoint,
        tuple(sorted(headers.items())),  # type: ignore[arg-type]
    )


def _valid_stdio_command(command: str, entries: tuple[FrozenPackageEntry, ...]) -> bool:
    if "\x00" in command or not command:
        return False
    if "/" not in command and "\\" not in command:
        return True
    if "\\" in command or not command.startswith("./"):
        return False
    relative = PurePosixPath(command[2:])
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        return False
    entry = next((item for item in entries if item.relative_path == relative), None)
    return bool(
        entry is not None
        and entry.kind is PackageEntryKind.REGULAR_FILE
        and entry.executable
    )


def _valid_portable_cwd(value: str) -> bool:
    prefixes = ("./", "${PLUGIN_ROOT}", "${PLUGIN_DATA}")
    prefix = next((item for item in prefixes if value.startswith(item)), None)
    if prefix is None:
        return False
    suffix = value[len(prefix) :].lstrip("/")
    return all(item not in {"", ".", ".."} for item in PurePosixPath(suffix).parts)


def _valid_http_endpoint(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return False
    if parsed.scheme == "https":
        return True
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _valid_public_headers(value: Mapping[str, object]) -> bool:
    folded: set[str] = set()
    for name, item in value.items():
        try:
            encoded_name = name.encode("ascii")
            encoded_value = item.encode("ascii") if isinstance(item, str) else b""
        except UnicodeEncodeError:
            return False
        if (
            not name
            or any(char not in _TCHAR for char in encoded_name)
            or name.casefold() in folded
            or not isinstance(item, str)
            or item != item.strip(" \t")
            or any(
                char not in {9, 32} and not 33 <= char <= 126 for char in encoded_value
            )
        ):
            return False
        folded.add(name.casefold())
    return True


def _parse_hooks(
    root_fd: int,
    entries: tuple[FrozenPackageEntry, ...],
    *,
    source: Path,
    manifest: PluginManifest,
    package_install_id: str,
    visibility: HookVisibilityScope,
    workspace_state_key: str | None,
    lifetime_anchor: object | None,
    declaration_environment: tuple[tuple[str, str], ...],
    deadline_monotonic: float,
    cancellation: PluginCancellationPort,
) -> tuple[PluginComponentSummary, ParsedHookConfig | None]:
    relative = PurePosixPath("dev.pulsara/hooks/hooks.json")
    entry = next((item for item in entries if item.relative_path == relative), None)
    if entry is None:
        return PluginComponentSummary(
            PluginComponentObservationDisposition.MISSING
        ), None
    if entry.kind is not PackageEntryKind.REGULAR_FILE:
        diagnostic = _diagnostic(
            PluginDiagnosticCode.COMPONENT_KIND_INVALID,
            source.joinpath(*relative.parts),
        )
        return (
            PluginComponentSummary(
                PluginComponentObservationDisposition.INVALID,
                diagnostics=(diagnostic,),
            ),
            None,
        )
    try:
        raw = _read_bounded_entry(
            root_fd,
            entry,
            maximum=MAXIMUM_HOOK_CONFIG_BYTES,
            deadline_monotonic=deadline_monotonic,
            cancellation=cancellation,
        )
        identity = PluginHookSourceIdentity(
            visibility,
            manifest.name,
            package_install_id,
            relative.as_posix(),
            source.joinpath(*relative.parts),
            workspace_state_key,
            lifetime_anchor,
        )
        subject = PluginHookTrustSubject(visibility, manifest.name, workspace_state_key)
        provenance = FrozenHookSourceProvenance(
            identity,
            subject,
            None,
            f"PLUGIN:{manifest.name}",
            declaration_environment,
            lifetime_anchor,
        )
        parsed = parse_hook_config(raw, provenance=provenance)
    except (HookConfigParseError, JsonBoundExceeded):
        diagnostic = HookDiagnostic(
            "HOOK_SOURCE_PARSE_FAILED",
            "Plugin Hook source failed deterministic parsing",
            source_label=f"PLUGIN:{manifest.name}",
        )
        return (
            PluginComponentSummary(
                PluginComponentObservationDisposition.INVALID,
                diagnostics=(diagnostic,),
            ),
            None,
        )
    summaries = tuple(
        PluginHookDefinitionSummary(
            item.event_type.external_name,
            item.matcher.pattern,
            item.command,
            item.command_windows,
            item.timeout_seconds,
            item.asynchronous,
            item.status_message,
        )
        for item in parsed.definitions
    )
    return (
        PluginComponentSummary(
            PluginComponentObservationDisposition.COMPLETE,
            hook_definitions=summaries,
            diagnostics=parsed.diagnostics,
        ),
        parsed,
    )


def _stable_secret_scan(
    boundary: ProcessCredentialBoundary,
    root_fd: int,
    entries: tuple[FrozenPackageEntry, ...],
    *,
    source_path: bytes,
    deadline_monotonic: float,
    cancellation: PluginCancellationPort,
    scrub_set: ProcessCredentialScrubSet,
) -> None:
    while True:
        _check_abort(deadline_monotonic, cancellation)
        try:
            with boundary.sync_guard(
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            ) as guard:
                expected = guard.value
                scrub_set.observe(expected)
        except ProcessCredentialBoundaryCancelled as exc:
            raise PluginPackageCancelled from exc
        except ProcessCredentialBoundaryTimedOut as exc:
            raise PluginPackageTimedOut from exc
        for encoded in scrub_set.values:
            if encoded in source_path or _tree_contains(
                root_fd,
                entries,
                encoded,
                deadline_monotonic,
                cancellation,
            ):
                raise PluginPackageInvalid(
                    (_diagnostic(PluginDiagnosticCode.SOURCE_CONTAINS_ACTIVE_API_KEY),)
                )
        try:
            with boundary.sync_guard(
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            ) as guard:
                scrub_set.observe(guard.value)
                if guard.value == expected:
                    return
        except ProcessCredentialBoundaryCancelled as exc:
            raise PluginPackageCancelled from exc
        except ProcessCredentialBoundaryTimedOut as exc:
            raise PluginPackageTimedOut from exc


def tree_contains_secret(
    root_fd: int,
    entries: tuple[FrozenPackageEntry, ...],
    secret: bytes,
    *,
    deadline_monotonic: float,
    cancellation: PluginCancellationPort,
) -> bool:
    return _tree_contains(root_fd, entries, secret, deadline_monotonic, cancellation)


def _tree_contains(
    root_fd: int,
    entries: tuple[FrozenPackageEntry, ...],
    secret: bytes,
    deadline_monotonic: float,
    cancellation: PluginCancellationPort,
) -> bool:
    if not secret:
        return False
    for entry in entries:
        _check_abort(deadline_monotonic, cancellation)
        if secret in os.fsencode(entry.relative_path.as_posix()):
            return True
        if entry.kind is not PackageEntryKind.REGULAR_FILE:
            continue
        descriptor = _open_regular_entry(root_fd, entry)
        try:
            tail = b""
            while True:
                _check_abort(deadline_monotonic, cancellation)
                try:
                    chunk = os.read(descriptor, COPY_CHUNK_BYTES)
                except OSError as exc:
                    raise PluginPackageUnavailable(
                        PluginDiagnosticCode.SOURCE_UNAVAILABLE
                    ) from exc
                if not chunk:
                    break
                combined = tail + chunk
                if secret in combined:
                    return True
                tail = combined[-(len(secret) - 1) :] if len(secret) > 1 else b""
            if not _entry_matches(entry, os.fstat(descriptor)):
                raise PluginPackageRaced
        finally:
            os.close(descriptor)
    return False


def revalidate_observation(
    observation: HeldPluginPackageObservation,
    *,
    deadline_monotonic: float,
    cancellation: PluginCancellationPort,
) -> None:
    _revalidate_source(
        observation.source_path,
        observation.descriptor,
        root=(observation.root_device, observation.root_inode),
        expected_entries=observation.entries,
        deadline_monotonic=deadline_monotonic,
        cancellation=cancellation,
        enforce_managed_admission=observation.enforce_managed_admission,
    )


def _revalidate_source(
    source: Path,
    root_fd: int,
    *,
    root: tuple[int, int],
    expected_entries: tuple[FrozenPackageEntry, ...],
    deadline_monotonic: float,
    cancellation: PluginCancellationPort,
    enforce_managed_admission: bool,
) -> None:
    _check_abort(deadline_monotonic, cancellation)
    try:
        held = os.fstat(root_fd)
        rebound = open_absolute_directory_nofollow(source)
    except FileNotFoundError as exc:
        raise PluginPackageRaced from exc
    except OSError as exc:
        raise PluginPackageUnavailable(PluginDiagnosticCode.SOURCE_UNAVAILABLE) from exc
    try:
        rebound_stat = os.fstat(rebound)
        if (held.st_dev, held.st_ino) != root or (
            rebound_stat.st_dev,
            rebound_stat.st_ino,
        ) != root:
            raise PluginPackageRaced
    finally:
        os.close(rebound)
    actual = _observe_membership(
        root_fd,
        deadline_monotonic=deadline_monotonic,
        cancellation=cancellation,
        enforce_managed_admission=enforce_managed_admission,
    )
    if actual != expected_entries:
        raise PluginPackageRaced


def _schema_validator(name: str, uri: str) -> Draft202012Validator:
    schema = _load_schema(name)
    registry = Registry().with_resource(uri, Resource.from_contents(schema))
    return Draft202012Validator(schema, registry=registry)


def _load_schema(name: str) -> dict[str, object]:
    raw = (
        resources.files("pulsara_agent.plugins.schemas")
        .joinpath("1.0.0", name)
        .read_bytes()
    )
    value = json.loads(raw)
    if not isinstance(value, dict):  # pragma: no cover - packaged invariant
        raise RuntimeError("embedded Agent Plugins schema is invalid")
    return value


def _check_abort(
    deadline_monotonic: float, cancellation: PluginCancellationPort
) -> None:
    if cancellation.cancellation_requested():
        raise PluginPackageCancelled
    if monotonic() >= deadline_monotonic:
        raise PluginPackageTimedOut


def _diagnostic(
    code: PluginDiagnosticCode,
    path: Path | None = None,
    *,
    component: str | None = None,
) -> PluginDiagnostic:
    return PluginDiagnostic(
        code,
        code.value.replace("plugin_", "").replace("_", " "),
        None if path is None else str(path),
        component,
    )


def _is_utf8(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


__all__ = [
    "COPY_CHUNK_BYTES",
    "FrozenPackageEntry",
    "HeldPluginPackageObservation",
    "PackageEntryKind",
    "ParsedPluginMcpComponent",
    "ParsedPluginSkillCandidate",
    "PluginPackageCancelled",
    "PluginPackageInvalid",
    "PluginPackageRaced",
    "PluginPackageTimedOut",
    "PluginPackageUnavailable",
    "PluginSourceObserver",
    "revalidate_observation",
    "tree_contains_secret",
]
