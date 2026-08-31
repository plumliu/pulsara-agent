"""Descriptor-relative atomic publisher for loose local Skills."""

from __future__ import annotations

from dataclasses import dataclass
import errno
from enum import StrEnum
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Protocol
from uuid import uuid4

from pulsara_agent.capability.local_skills import (
    MAX_SKILL_FILE_BYTES,
    SKILL_FILE_NAME,
    ParsedSkillDocument,
    diagnostic_at,
    parse_skill_document,
    validate_skill_candidate_placement,
)
from pulsara_agent.local_source_binding import (
    open_absolute_directory_nofollow,
    prepare_local_source_path,
)
from pulsara_agent.capability.pulsara_home import (
    PulsaraHomeDisposition,
    PulsaraHomeResolution,
    PulsaraHomeUnavailableReason,
)
from pulsara_agent.capability.types import (
    SkillDiagnostic,
    SkillDiagnosticCode,
    SkillDiagnosticSeverity,
)
from pulsara_agent.exclusive_publish import (
    ExclusiveDirectoryPublisher,
    ExclusivePublishPrimitiveUnavailable,
    PlatformExclusiveDirectoryPublisher,
)


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_READ_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_WRITE_FILE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_COPY_CHUNK_BYTES = 64 * 1024


class LocalSkillInstallScope(StrEnum):
    WORKSPACE = "workspace"
    USER = "user"


class LocalSkillInstallDisposition(StrEnum):
    INSTALLED = "INSTALLED"
    SOURCE_INVALID = "SOURCE_INVALID"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SOURCE_RACED = "SOURCE_RACED"
    TARGET_CONFIGURATION_UNAVAILABLE = "TARGET_CONFIGURATION_UNAVAILABLE"
    UNSUPPORTED_ENTRY = "UNSUPPORTED_ENTRY"
    DESTINATION_EXISTS = "DESTINATION_EXISTS"
    STAGING_UNAVAILABLE = "STAGING_UNAVAILABLE"
    PUBLISH_UNAVAILABLE = "PUBLISH_UNAVAILABLE"
    CANCELLED = "CANCELLED"
    CLEANUP_UNAVAILABLE = "CLEANUP_UNAVAILABLE"


class LocalSkillPublishUnavailableReason(StrEnum):
    UNSUPPORTED_EXCLUSIVE_PRIMITIVE = "UNSUPPORTED_EXCLUSIVE_PRIMITIVE"
    ROOT_BINDING_RACED = "ROOT_BINDING_RACED"
    PUBLISH_IO_FAILURE = "PUBLISH_IO_FAILURE"


class LocalSkillCleanupLocationStatus(StrEnum):
    KNOWN_AT_LAST_OBSERVATION = "KNOWN_AT_LAST_OBSERVATION"
    UNRESOLVED_AFTER_NAMESPACE_INTERFERENCE = "UNRESOLVED_AFTER_NAMESPACE_INTERFERENCE"


@dataclass(frozen=True, slots=True)
class LocalSkillInstallOutcome:
    disposition: LocalSkillInstallDisposition
    source_path: Path
    destination_path: Path | None = None
    diagnostics: tuple[SkillDiagnostic, ...] = ()
    target_configuration_reason: PulsaraHomeUnavailableReason | None = None
    publish_unavailable_reason: LocalSkillPublishUnavailableReason | None = None
    entry_path: PurePosixPath | None = None
    prior_disposition: LocalSkillInstallDisposition | None = None
    attempted_staging_path: Path | None = None
    cleanup_location_status: LocalSkillCleanupLocationStatus | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, LocalSkillInstallDisposition):
            raise TypeError("Skill installation disposition is not closed")
        if not self.source_path.is_absolute():
            raise ValueError("Skill installation source path is not absolute")
        extras = {
            "destination": self.destination_path is not None,
            "diagnostics": bool(self.diagnostics),
            "configuration": self.target_configuration_reason is not None,
            "publish": self.publish_unavailable_reason is not None,
            "entry": self.entry_path is not None,
            "prior": self.prior_disposition is not None,
            "staging": self.attempted_staging_path is not None,
            "cleanup": self.cleanup_location_status is not None,
        }
        allowed: dict[LocalSkillInstallDisposition, frozenset[str]] = {
            LocalSkillInstallDisposition.INSTALLED: frozenset({"destination"}),
            LocalSkillInstallDisposition.SOURCE_INVALID: frozenset({"diagnostics"}),
            LocalSkillInstallDisposition.TARGET_CONFIGURATION_UNAVAILABLE: frozenset(
                {"configuration"}
            ),
            LocalSkillInstallDisposition.PUBLISH_UNAVAILABLE: frozenset({"publish"}),
            LocalSkillInstallDisposition.UNSUPPORTED_ENTRY: frozenset({"entry"}),
            LocalSkillInstallDisposition.CLEANUP_UNAVAILABLE: frozenset(
                {"prior", "staging", "cleanup"}
            ),
            LocalSkillInstallDisposition.SOURCE_UNAVAILABLE: frozenset(),
            LocalSkillInstallDisposition.SOURCE_RACED: frozenset(),
            LocalSkillInstallDisposition.DESTINATION_EXISTS: frozenset(),
            LocalSkillInstallDisposition.STAGING_UNAVAILABLE: frozenset(),
            LocalSkillInstallDisposition.CANCELLED: frozenset(),
        }
        present = frozenset(name for name, value in extras.items() if value)
        if present != allowed[self.disposition]:
            raise ValueError("Skill installation outcome field combination conflicts")
        if (
            self.destination_path is not None
            and not self.destination_path.is_absolute()
        ):
            raise ValueError("Skill installation destination is not absolute")
        if self.disposition is LocalSkillInstallDisposition.SOURCE_INVALID and not all(
            isinstance(item.code, SkillDiagnosticCode) for item in self.diagnostics
        ):
            raise ValueError("Skill installation diagnostics are not closed")
        if self.prior_disposition is LocalSkillInstallDisposition.CLEANUP_UNAVAILABLE:
            raise ValueError("cleanup outcome cannot wrap another cleanup outcome")


class LocalSkillCancellationProbe(Protocol):
    def cancellation_requested(self) -> bool: ...


class NeverCancelLocalSkillOperation:
    def cancellation_requested(self) -> bool:
        return False


class _EntryKind(StrEnum):
    DIRECTORY = "DIRECTORY"
    REGULAR_FILE = "REGULAR_FILE"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class _FrozenSourceEntry:
    relative_path: PurePosixPath
    kind: _EntryKind
    device: int
    inode: int
    size: int | None = None
    mtime_ns: int | None = None
    ctime_ns: int | None = None
    executable_bits: int | None = None


@dataclass(frozen=True, slots=True)
class _FrozenSourceObservation:
    source_path: Path
    root_device: int
    root_inode: int
    entries: tuple[_FrozenSourceEntry, ...]
    skill_document_bytes: bytes
    parsed: ParsedSkillDocument


@dataclass(frozen=True, slots=True)
class _TargetRoot:
    path: Path
    descriptor: int
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _StageBinding:
    name: str
    path: Path
    descriptor: int
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _FrozenStageEntry:
    relative_path: PurePosixPath
    kind: _EntryKind
    device: int
    inode: int
    size: int | None
    mtime_ns: int
    ctime_ns: int
    mode: int
    content_digest: bytes | None


@dataclass(frozen=True, slots=True)
class _FrozenStageEvidence:
    root_device: int
    root_inode: int
    root_mtime_ns: int
    root_ctime_ns: int
    root_mode: int
    entries: tuple[_FrozenStageEntry, ...]


class _SourceRaced(OSError):
    pass


class _SourceUnavailable(OSError):
    pass


class _StageUnavailable(OSError):
    pass


class _Cancelled(RuntimeError):
    pass


class _RootBindingRaced(OSError):
    pass


class _StageBindingRaced(OSError):
    pass


class _PublishIoUnavailable(OSError):
    pass


class _StageCreationCleanupUnavailable(OSError):
    def __init__(
        self,
        stage_name: str,
        location_status: LocalSkillCleanupLocationStatus,
    ) -> None:
        super().__init__(stage_name)
        self.stage_name = stage_name
        self.location_status = location_status


class AtomicLocalSkillPublisher:
    """Single-attempt source observation, copy, verification, and publish owner."""

    def __init__(
        self,
        *,
        exclusive_publisher: ExclusiveDirectoryPublisher | None = None,
    ) -> None:
        self._exclusive = exclusive_publisher or PlatformExclusiveDirectoryPublisher()

    def install(
        self,
        *,
        source_path: Path,
        scope: LocalSkillInstallScope,
        workspace_root: Path | None,
        pulsara_home: PulsaraHomeResolution | None,
        cancellation: LocalSkillCancellationProbe | None = None,
    ) -> LocalSkillInstallOutcome:
        source = prepare_local_source_path(source_path)
        probe = cancellation or NeverCancelLocalSkillOperation()
        if not isinstance(scope, LocalSkillInstallScope):
            raise TypeError("Skill installation scope is not closed")
        target_configuration = self._target_configuration(
            scope=scope,
            workspace_root=workspace_root,
            pulsara_home=pulsara_home,
        )
        if isinstance(target_configuration, PulsaraHomeUnavailableReason):
            return LocalSkillInstallOutcome(
                LocalSkillInstallDisposition.TARGET_CONFIGURATION_UNAVAILABLE,
                source,
                target_configuration_reason=target_configuration,
            )
        (
            target_root_path,
            target_anchor_path,
            target_components,
            create_anchor,
        ) = target_configuration

        try:
            source_fd = open_absolute_directory_nofollow(source)
        except FileNotFoundError:
            return LocalSkillInstallOutcome(
                LocalSkillInstallDisposition.SOURCE_UNAVAILABLE,
                source,
            )
        except OSError:
            return LocalSkillInstallOutcome(
                LocalSkillInstallDisposition.SOURCE_UNAVAILABLE,
                source,
            )
        try:
            try:
                root_stat = os.fstat(source_fd)
            except OSError:
                return LocalSkillInstallOutcome(
                    LocalSkillInstallDisposition.SOURCE_UNAVAILABLE,
                    source,
                )
            try:
                entries = _observe_source_membership(source_fd, probe=probe)
                skill_entry = next(
                    (
                        item
                        for item in entries
                        if item.relative_path == PurePosixPath(SKILL_FILE_NAME)
                    ),
                    None,
                )
                if (
                    skill_entry is not None
                    and skill_entry.kind is not _EntryKind.REGULAR_FILE
                ):
                    return LocalSkillInstallOutcome(
                        LocalSkillInstallDisposition.UNSUPPORTED_ENTRY,
                        source,
                        entry_path=skill_entry.relative_path,
                    )
                skill_bytes = _read_source_skill_document(
                    source_fd,
                    entries,
                    maximum=MAX_SKILL_FILE_BYTES,
                    probe=probe,
                )
            except _Cancelled:
                return LocalSkillInstallOutcome(
                    LocalSkillInstallDisposition.CANCELLED,
                    source,
                )
            except _SourceRaced:
                return LocalSkillInstallOutcome(
                    LocalSkillInstallDisposition.SOURCE_RACED,
                    source,
                )
            except _SourceUnavailable:
                return LocalSkillInstallOutcome(
                    LocalSkillInstallDisposition.SOURCE_UNAVAILABLE,
                    source,
                )
            except MemoryError:
                return LocalSkillInstallOutcome(
                    LocalSkillInstallDisposition.SOURCE_UNAVAILABLE,
                    source,
                )
            if skill_bytes is None:
                return LocalSkillInstallOutcome(
                    LocalSkillInstallDisposition.SOURCE_INVALID,
                    source,
                    diagnostics=(
                        SkillDiagnostic(
                            severity=SkillDiagnosticSeverity.WARNING,
                            code=SkillDiagnosticCode.MISSING_DOCUMENT,
                            message="Skill source is missing SKILL.md",
                            path=source / SKILL_FILE_NAME,
                        ),
                    ),
                )
            try:
                parsed_result = parse_skill_document(skill_bytes)
            except MemoryError:
                return LocalSkillInstallOutcome(
                    LocalSkillInstallDisposition.SOURCE_UNAVAILABLE,
                    source,
                )
            if parsed_result.parsed is None:
                diagnostics = tuple(
                    diagnostic_at(item, source / SKILL_FILE_NAME)
                    for item in parsed_result.diagnostics
                )
                return LocalSkillInstallOutcome(
                    LocalSkillInstallDisposition.SOURCE_INVALID,
                    source,
                    diagnostics=diagnostics,
                )
            unsupported = next(
                (item for item in entries if item.kind is _EntryKind.UNSUPPORTED),
                None,
            )
            if unsupported is not None:
                return LocalSkillInstallOutcome(
                    LocalSkillInstallDisposition.UNSUPPORTED_ENTRY,
                    source,
                    entry_path=unsupported.relative_path,
                )
            observation = _FrozenSourceObservation(
                source_path=source,
                root_device=root_stat.st_dev,
                root_inode=root_stat.st_ino,
                entries=entries,
                skill_document_bytes=skill_bytes,
                parsed=parsed_result.parsed,
            )
            if _paths_overlap(source, target_root_path):
                return LocalSkillInstallOutcome(
                    LocalSkillInstallDisposition.STAGING_UNAVAILABLE,
                    source,
                )
            if probe.cancellation_requested():
                return LocalSkillInstallOutcome(
                    LocalSkillInstallDisposition.CANCELLED,
                    source,
                )
            try:
                target = _prepare_target_root(
                    target_root_path=target_root_path,
                    anchor_path=target_anchor_path,
                    control_components=target_components,
                    create_anchor=create_anchor,
                )
            except (MemoryError, OSError):
                return LocalSkillInstallOutcome(
                    LocalSkillInstallDisposition.STAGING_UNAVAILABLE,
                    source,
                )
            try:
                final_name = observation.parsed.name
                try:
                    destination_exists = _entry_exists(target.descriptor, final_name)
                except (MemoryError, OSError):
                    return LocalSkillInstallOutcome(
                        LocalSkillInstallDisposition.STAGING_UNAVAILABLE,
                        source,
                    )
                if destination_exists:
                    return LocalSkillInstallOutcome(
                        LocalSkillInstallDisposition.DESTINATION_EXISTS,
                        source,
                    )
                if probe.cancellation_requested():
                    return LocalSkillInstallOutcome(
                        LocalSkillInstallDisposition.CANCELLED,
                        source,
                    )
                try:
                    stage = _create_stage(target)
                except _StageCreationCleanupUnavailable as exc:
                    return LocalSkillInstallOutcome(
                        LocalSkillInstallDisposition.CLEANUP_UNAVAILABLE,
                        source,
                        prior_disposition=(
                            LocalSkillInstallDisposition.STAGING_UNAVAILABLE
                        ),
                        attempted_staging_path=target.path / exc.stage_name,
                        cleanup_location_status=exc.location_status,
                    )
                except (MemoryError, OSError):
                    return LocalSkillInstallOutcome(
                        LocalSkillInstallDisposition.STAGING_UNAVAILABLE,
                        source,
                    )
                try:
                    try:
                        _copy_source_to_stage(
                            source_fd,
                            stage.descriptor,
                            observation,
                            probe,
                        )
                        stage_evidence = _verify_stage_against_source(
                            source_fd,
                            stage.descriptor,
                            observation,
                            probe,
                        )
                        _revalidate_source(source_fd, observation, probe=probe)
                    except _Cancelled:
                        prior = LocalSkillInstallOutcome(
                            LocalSkillInstallDisposition.CANCELLED,
                            source,
                        )
                        return _cleanup_or_wrap(target, stage, prior)
                    except _SourceRaced:
                        prior = LocalSkillInstallOutcome(
                            LocalSkillInstallDisposition.SOURCE_RACED,
                            source,
                        )
                        return _cleanup_or_wrap(target, stage, prior)
                    except _SourceUnavailable:
                        prior = LocalSkillInstallOutcome(
                            LocalSkillInstallDisposition.SOURCE_UNAVAILABLE,
                            source,
                        )
                        return _cleanup_or_wrap(target, stage, prior)
                    except (OSError, _StageUnavailable):
                        disposition = _arbitrate_stage_failure(source_fd, observation)
                        prior = LocalSkillInstallOutcome(disposition, source)
                        return _cleanup_or_wrap(target, stage, prior)

                    try:
                        staged_skill_bytes = _read_bounded_stage_file(
                            stage.descriptor,
                            PurePosixPath(SKILL_FILE_NAME),
                            maximum=MAX_SKILL_FILE_BYTES,
                            probe=probe,
                        )
                        if staged_skill_bytes != observation.skill_document_bytes:
                            raise _StageUnavailable("staged SKILL.md bytes conflict")
                        staged_parse = parse_skill_document(staged_skill_bytes)
                        staged_placement = (
                            validate_skill_candidate_placement(
                                staged_parse.parsed, final_name
                            )
                            if staged_parse.parsed is not None
                            else None
                        )
                        if (
                            staged_parse.parsed != observation.parsed
                            or staged_placement is None
                            or not staged_placement.valid
                        ):
                            raise _StageUnavailable("staged Skill validation conflicts")
                    except _Cancelled:
                        prior = LocalSkillInstallOutcome(
                            LocalSkillInstallDisposition.CANCELLED,
                            source,
                        )
                        return _cleanup_or_wrap(target, stage, prior)
                    except (OSError, _StageUnavailable):
                        prior = LocalSkillInstallOutcome(
                            LocalSkillInstallDisposition.STAGING_UNAVAILABLE,
                            source,
                        )
                        return _cleanup_or_wrap(target, stage, prior)

                    if probe.cancellation_requested():
                        prior = LocalSkillInstallOutcome(
                            LocalSkillInstallDisposition.CANCELLED,
                            source,
                        )
                        return _cleanup_or_wrap(target, stage, prior)
                    try:
                        _final_binding_cut(
                            target,
                            stage,
                            final_name,
                            stage_evidence,
                            probe,
                        )
                    except _Cancelled:
                        prior = LocalSkillInstallOutcome(
                            LocalSkillInstallDisposition.CANCELLED,
                            source,
                        )
                        return _cleanup_or_wrap(target, stage, prior)
                    except _RootBindingRaced:
                        prior = LocalSkillInstallOutcome(
                            LocalSkillInstallDisposition.PUBLISH_UNAVAILABLE,
                            source,
                            publish_unavailable_reason=(
                                LocalSkillPublishUnavailableReason.ROOT_BINDING_RACED
                            ),
                        )
                        return _cleanup_or_wrap(target, stage, prior)
                    except _StageBindingRaced:
                        prior = LocalSkillInstallOutcome(
                            LocalSkillInstallDisposition.STAGING_UNAVAILABLE,
                            source,
                        )
                        return _cleanup_or_wrap(target, stage, prior)
                    except _StageUnavailable:
                        prior = LocalSkillInstallOutcome(
                            LocalSkillInstallDisposition.STAGING_UNAVAILABLE,
                            source,
                        )
                        return _cleanup_or_wrap(target, stage, prior)
                    except _PublishIoUnavailable:
                        prior = LocalSkillInstallOutcome(
                            LocalSkillInstallDisposition.PUBLISH_UNAVAILABLE,
                            source,
                            publish_unavailable_reason=(
                                LocalSkillPublishUnavailableReason.PUBLISH_IO_FAILURE
                            ),
                        )
                        return _cleanup_or_wrap(target, stage, prior)
                    except FileExistsError:
                        prior = LocalSkillInstallOutcome(
                            LocalSkillInstallDisposition.DESTINATION_EXISTS,
                            source,
                        )
                        return _cleanup_or_wrap(target, stage, prior)
                    if probe.cancellation_requested():
                        prior = LocalSkillInstallOutcome(
                            LocalSkillInstallDisposition.CANCELLED,
                            source,
                        )
                        return _cleanup_or_wrap(target, stage, prior)
                    try:
                        self._exclusive.publish(
                            target.descriptor, stage.name, final_name
                        )
                    except ExclusivePublishPrimitiveUnavailable:
                        prior = LocalSkillInstallOutcome(
                            LocalSkillInstallDisposition.PUBLISH_UNAVAILABLE,
                            source,
                            publish_unavailable_reason=(
                                LocalSkillPublishUnavailableReason.UNSUPPORTED_EXCLUSIVE_PRIMITIVE
                            ),
                        )
                        return _cleanup_or_wrap(target, stage, prior)
                    except OSError as exc:
                        if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                            prior = LocalSkillInstallOutcome(
                                LocalSkillInstallDisposition.DESTINATION_EXISTS,
                                source,
                            )
                        else:
                            prior = LocalSkillInstallOutcome(
                                LocalSkillInstallDisposition.PUBLISH_UNAVAILABLE,
                                source,
                                publish_unavailable_reason=(
                                    LocalSkillPublishUnavailableReason.PUBLISH_IO_FAILURE
                                ),
                            )
                        return _cleanup_or_wrap(target, stage, prior)
                    return LocalSkillInstallOutcome(
                        LocalSkillInstallDisposition.INSTALLED,
                        source,
                        destination_path=target.path / final_name,
                    )
                except MemoryError:
                    prior = LocalSkillInstallOutcome(
                        _arbitrate_allocation_failure(source_fd, observation),
                        source,
                    )
                    return _cleanup_or_wrap(target, stage, prior)
                finally:
                    os.close(stage.descriptor)
            finally:
                os.close(target.descriptor)
        finally:
            os.close(source_fd)

    @staticmethod
    def _target_configuration(
        *,
        scope: LocalSkillInstallScope,
        workspace_root: Path | None,
        pulsara_home: PulsaraHomeResolution | None,
    ) -> tuple[Path, Path, tuple[str, ...], bool] | PulsaraHomeUnavailableReason:
        if scope is LocalSkillInstallScope.WORKSPACE:
            if workspace_root is None:
                raise ValueError("workspace Skill installation requires a workspace")
            workspace = _lexical_absolute(workspace_root)
            return (
                workspace / ".pulsara" / "skills",
                workspace,
                (".pulsara", "skills"),
                False,
            )
        if pulsara_home is None:
            raise ValueError("user Skill installation requires Pulsara home resolution")
        if pulsara_home.disposition is PulsaraHomeDisposition.INVALID:
            reason = pulsara_home.unavailable_reason
            if reason is None:  # pragma: no cover - resolution invariant
                raise RuntimeError("invalid Pulsara home has no reason")
            return reason
        if pulsara_home.path is None:  # pragma: no cover - resolution invariant
            raise RuntimeError("resolved Pulsara home has no path")
        home = pulsara_home.path
        if pulsara_home.used_default:
            return (
                home / "skills",
                home.parent,
                (home.name, "skills"),
                False,
            )
        return (home / "skills", home, ("skills",), True)


def _lexical_absolute(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return Path(os.path.normpath(os.fspath(expanded)))


def _open_or_create_absolute_directory(path: Path) -> int:
    if not path.is_absolute():
        raise ValueError("absolute directory path required")
    current = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            created = False
            try:
                next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=current)
                created = True
                next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            if created:
                os.fchmod(next_fd, 0o700)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def _prepare_target_root(
    *,
    target_root_path: Path,
    anchor_path: Path,
    control_components: tuple[str, ...],
    create_anchor: bool,
) -> _TargetRoot:
    if target_root_path != anchor_path.joinpath(*control_components):
        raise ValueError("target root configuration is inconsistent")
    anchor_fd = (
        _open_or_create_absolute_directory(anchor_path)
        if create_anchor
        else open_absolute_directory_nofollow(anchor_path)
    )
    current = anchor_fd
    try:
        for component in control_components:
            created = False
            try:
                next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=current)
                created = True
                next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            if created:
                os.fchmod(next_fd, 0o700)
            os.close(current)
            current = next_fd
        metadata = os.fstat(current)
        return _TargetRoot(
            path=target_root_path,
            descriptor=current,
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )
    except BaseException:
        os.close(current)
        raise


def _paths_overlap(source: Path, target_root: Path) -> bool:
    try:
        source.relative_to(target_root)
        return True
    except ValueError:
        pass
    try:
        target_root.relative_to(source)
        return True
    except ValueError:
        return False


def _observe_source_membership(
    root_fd: int,
    *,
    probe: LocalSkillCancellationProbe | None = None,
) -> tuple[_FrozenSourceEntry, ...]:
    entries: list[_FrozenSourceEntry] = []
    operation_probe = probe or NeverCancelLocalSkillOperation()

    def walk(directory_fd: int, prefix: PurePosixPath) -> None:
        _check_cancel(operation_probe)
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise _SourceUnavailable from exc
        for name in names:
            _check_cancel(operation_probe)
            if name in {".", ".."}:
                raise _SourceRaced("invalid directory membership")
            relative = prefix / name if prefix.parts else PurePosixPath(name)
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise _SourceRaced from exc
            except OSError as exc:
                raise _SourceUnavailable from exc
            if stat.S_ISDIR(metadata.st_mode):
                if name == "__pycache__":
                    continue
                entry = _source_entry(relative, _EntryKind.DIRECTORY, metadata)
                entries.append(entry)
                try:
                    child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                except FileNotFoundError as exc:
                    raise _SourceRaced from exc
                except OSError as exc:
                    raise _SourceUnavailable from exc
                try:
                    if not _entry_matches_stat(entry, os.fstat(child_fd)):
                        raise _SourceRaced("source directory identity changed")
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                if name == ".DS_Store":
                    continue
                entries.append(
                    _source_entry(relative, _EntryKind.REGULAR_FILE, metadata)
                )
            else:
                entries.append(
                    _source_entry(relative, _EntryKind.UNSUPPORTED, metadata)
                )

    try:
        walk(root_fd, PurePosixPath())
    except RecursionError as exc:
        raise _SourceUnavailable(
            "source directory depth exhausted the host stack"
        ) from exc
    return tuple(entries)


def _source_entry(
    relative: PurePosixPath,
    kind: _EntryKind,
    metadata: os.stat_result,
) -> _FrozenSourceEntry:
    regular = kind is _EntryKind.REGULAR_FILE
    return _FrozenSourceEntry(
        relative_path=relative,
        kind=kind,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size if regular else None,
        mtime_ns=metadata.st_mtime_ns if regular else None,
        ctime_ns=metadata.st_ctime_ns if regular else None,
        executable_bits=(metadata.st_mode & 0o111) if regular else None,
    )


def _entry_matches_stat(entry: _FrozenSourceEntry, metadata: os.stat_result) -> bool:
    if entry.kind is _EntryKind.DIRECTORY:
        return (
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_dev == entry.device
            and metadata.st_ino == entry.inode
        )
    if entry.kind is _EntryKind.REGULAR_FILE:
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_dev == entry.device
            and metadata.st_ino == entry.inode
            and metadata.st_size == entry.size
            and metadata.st_mtime_ns == entry.mtime_ns
            and metadata.st_ctime_ns == entry.ctime_ns
            and metadata.st_mode & 0o111 == entry.executable_bits
        )
    return not stat.S_ISDIR(metadata.st_mode) and not stat.S_ISREG(metadata.st_mode)


def _read_source_skill_document(
    root_fd: int,
    entries: tuple[_FrozenSourceEntry, ...],
    *,
    maximum: int,
    probe: LocalSkillCancellationProbe | None = None,
) -> bytes | None:
    expected = next(
        (
            item
            for item in entries
            if item.relative_path == PurePosixPath(SKILL_FILE_NAME)
        ),
        None,
    )
    if expected is None:
        return None
    if expected.kind is not _EntryKind.REGULAR_FILE:
        raise _SourceUnavailable("SKILL.md is not a regular file")
    return _read_bounded_source_file(
        root_fd,
        expected,
        maximum=maximum,
        probe=probe,
    )


def _read_bounded_source_file(
    root_fd: int,
    entry: _FrozenSourceEntry,
    *,
    maximum: int,
    probe: LocalSkillCancellationProbe | None = None,
) -> bytes:
    operation_probe = probe or NeverCancelLocalSkillOperation()
    parent_fd, name = _open_relative_parent(root_fd, entry.relative_path)
    try:
        try:
            file_fd = os.open(name, _READ_FILE_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError as exc:
            raise _SourceRaced from exc
        except OSError as exc:
            raise _SourceUnavailable from exc
        try:
            before = os.fstat(file_fd)
            if not _entry_matches_stat(entry, before):
                raise _SourceRaced("source file identity changed")
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining > 0:
                _check_cancel(operation_probe)
                amount = min(_COPY_CHUNK_BYTES, remaining)
                try:
                    chunk = os.read(file_fd, amount)
                except OSError as exc:
                    raise _SourceUnavailable from exc
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if not _entry_matches_stat(entry, os.fstat(file_fd)):
                raise _SourceRaced("source file changed during read")
            return b"".join(chunks)
        finally:
            os.close(file_fd)
    finally:
        os.close(parent_fd)


def _open_relative_parent(root_fd: int, relative: PurePosixPath) -> tuple[int, str]:
    parts = relative.parts
    if not parts:
        raise ValueError("relative file path is empty")
    current = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = next_fd
        return current, parts[-1]
    except BaseException:
        os.close(current)
        raise


def _entry_exists(root_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _create_stage(target: _TargetRoot) -> _StageBinding:
    name = f".pulsara-skill-install-{uuid4().hex}"
    try:
        os.mkdir(name, mode=0o700, dir_fd=target.descriptor)
    except MemoryError as exc:
        # A synthetic/faulting adapter may have completed the syscall before
        # failing to allocate its return path.  Without initial identity we
        # cannot safely remove an entry at this name.
        raise _StageCreationCleanupUnavailable(
            name,
            LocalSkillCleanupLocationStatus.UNRESOLVED_AFTER_NAMESPACE_INTERFERENCE,
        ) from exc
    try:
        metadata = os.stat(name, dir_fd=target.descriptor, follow_symlinks=False)
    except MemoryError as exc:
        raise _StageCreationCleanupUnavailable(
            name,
            LocalSkillCleanupLocationStatus.UNRESOLVED_AFTER_NAMESPACE_INTERFERENCE,
        ) from exc
    except OSError as exc:
        raise _StageCreationCleanupUnavailable(
            name,
            LocalSkillCleanupLocationStatus.KNOWN_AT_LAST_OBSERVATION,
        ) from exc
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=target.descriptor)
    except (MemoryError, OSError):
        _cleanup_failed_stage_creation(target, name, metadata)
        raise
    try:
        os.fchmod(descriptor, 0o700)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise _StageBindingRaced("staging binding changed during creation")
        return _StageBinding(
            name=name,
            path=target.path / name,
            descriptor=descriptor,
            device=opened.st_dev,
            inode=opened.st_ino,
        )
    except BaseException:
        os.close(descriptor)
        try:
            _cleanup_failed_stage_creation(target, name, metadata)
        except _StageCreationCleanupUnavailable:
            raise
        except (MemoryError, RecursionError) as exc:
            raise _StageCreationCleanupUnavailable(
                name,
                LocalSkillCleanupLocationStatus.KNOWN_AT_LAST_OBSERVATION,
            ) from exc
        raise


def _cleanup_failed_stage_creation(
    target: _TargetRoot,
    name: str,
    expected: os.stat_result,
) -> None:
    try:
        current = os.stat(name, dir_fd=target.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except (MemoryError, RecursionError, OSError) as exc:
        raise _StageCreationCleanupUnavailable(
            name,
            LocalSkillCleanupLocationStatus.KNOWN_AT_LAST_OBSERVATION,
        ) from exc
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
        expected.st_dev,
        expected.st_ino,
    ):
        raise _StageCreationCleanupUnavailable(
            name,
            LocalSkillCleanupLocationStatus.UNRESOLVED_AFTER_NAMESPACE_INTERFERENCE,
        )
    try:
        os.rmdir(name, dir_fd=target.descriptor)
    except (MemoryError, RecursionError, OSError) as exc:
        raise _StageCreationCleanupUnavailable(
            name,
            LocalSkillCleanupLocationStatus.KNOWN_AT_LAST_OBSERVATION,
        ) from exc


def _copy_source_to_stage(
    source_fd: int,
    stage_fd: int,
    observation: _FrozenSourceObservation,
    probe: LocalSkillCancellationProbe,
) -> None:
    for entry in observation.entries:
        _check_cancel(probe)
        if entry.kind is _EntryKind.DIRECTORY:
            parent_fd, name = _open_relative_parent(stage_fd, entry.relative_path)
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
                try:
                    os.fchmod(child_fd, 0o700)
                finally:
                    os.close(child_fd)
            except OSError as exc:
                raise _StageUnavailable from exc
            finally:
                os.close(parent_fd)
            continue
        if entry.kind is not _EntryKind.REGULAR_FILE:
            raise _StageUnavailable("unsupported source entry reached copy")
        _copy_regular_file(source_fd, stage_fd, entry, probe)


def _copy_regular_file(
    source_root_fd: int,
    stage_root_fd: int,
    entry: _FrozenSourceEntry,
    probe: LocalSkillCancellationProbe,
) -> None:
    source_parent, name = _open_relative_parent(source_root_fd, entry.relative_path)
    stage_parent, stage_name = _open_relative_parent(stage_root_fd, entry.relative_path)
    try:
        try:
            source_file = os.open(name, _READ_FILE_FLAGS, dir_fd=source_parent)
        except FileNotFoundError as exc:
            raise _SourceRaced from exc
        except OSError as exc:
            raise _SourceUnavailable from exc
        try:
            if not _entry_matches_stat(entry, os.fstat(source_file)):
                raise _SourceRaced("source file changed before copy")
            try:
                stage_file = os.open(
                    stage_name, _WRITE_FILE_FLAGS, 0o600, dir_fd=stage_parent
                )
            except OSError as exc:
                raise _StageUnavailable from exc
            try:
                try:
                    os.fchmod(stage_file, 0o600 | (entry.executable_bits or 0))
                except OSError as exc:
                    raise _StageUnavailable from exc
                while True:
                    _check_cancel(probe)
                    try:
                        chunk = os.read(source_file, _COPY_CHUNK_BYTES)
                    except OSError as exc:
                        raise _SourceUnavailable from exc
                    if not chunk:
                        break
                    _write_all(stage_file, chunk)
                if not _entry_matches_stat(entry, os.fstat(source_file)):
                    raise _SourceRaced("source file changed during copy")
            finally:
                os.close(stage_file)
        finally:
            os.close(source_file)
    finally:
        os.close(source_parent)
        os.close(stage_parent)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        try:
            written = os.write(descriptor, view[offset:])
        except OSError as exc:
            raise _StageUnavailable from exc
        if written <= 0:
            raise _StageUnavailable("staging write made no progress")
        offset += written


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _digest_source_file(
    root_fd: int,
    entry: _FrozenSourceEntry,
    *,
    probe: LocalSkillCancellationProbe,
) -> bytes:
    parent_fd, name = _open_relative_parent(root_fd, entry.relative_path)
    try:
        try:
            descriptor = os.open(name, _READ_FILE_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError as exc:
            raise _SourceRaced from exc
        except OSError as exc:
            raise _SourceUnavailable from exc
        try:
            before = os.fstat(descriptor)
            if not _entry_matches_stat(entry, before):
                raise _SourceRaced("source file changed before verification")
            digest = sha256()
            while True:
                _check_cancel(probe)
                try:
                    chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
                except OSError as exc:
                    raise _SourceUnavailable from exc
                if not chunk:
                    break
                digest.update(chunk)
            if not _entry_matches_stat(entry, os.fstat(descriptor)):
                raise _SourceRaced("source file changed during verification")
            return digest.digest()
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _digest_stage_file(
    root_fd: int,
    relative: PurePosixPath,
    *,
    expected_metadata: os.stat_result,
    probe: LocalSkillCancellationProbe,
) -> bytes:
    try:
        parent_fd, name = _open_relative_parent(root_fd, relative)
    except OSError as exc:
        raise _StageUnavailable from exc
    try:
        try:
            descriptor = os.open(name, _READ_FILE_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise _StageUnavailable from exc
        try:
            before = os.fstat(descriptor)
            expected_identity = _file_identity(expected_metadata)
            if (
                not stat.S_ISREG(before.st_mode)
                or _file_identity(before) != expected_identity
            ):
                raise _StageUnavailable("staged file identity changed")
            digest = sha256()
            while True:
                _check_cancel(probe)
                try:
                    chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
                except OSError as exc:
                    raise _StageUnavailable from exc
                if not chunk:
                    break
                digest.update(chunk)
            if _file_identity(os.fstat(descriptor)) != expected_identity:
                raise _StageUnavailable("staged file changed during verification")
            return digest.digest()
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _verify_stage_against_source(
    source_fd: int,
    stage_fd: int,
    observation: _FrozenSourceObservation,
    probe: LocalSkillCancellationProbe,
) -> _FrozenStageEvidence:
    actual = _observe_stage_membership(stage_fd, probe=probe)
    expected_shape = tuple(
        (item.relative_path, item.kind) for item in observation.entries
    )
    actual_shape = tuple((item[0], item[1]) for item in actual)
    if actual_shape != expected_shape:
        raise _StageUnavailable("staged membership conflicts")
    by_path = {path: metadata for path, _kind, metadata in actual}
    frozen_entries: list[_FrozenStageEntry] = []
    for entry in observation.entries:
        _check_cancel(probe)
        metadata = by_path[entry.relative_path]
        if entry.kind is _EntryKind.DIRECTORY:
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                raise _StageUnavailable("staged directory mode conflicts")
            frozen_entries.append(
                _freeze_stage_entry(entry.relative_path, entry.kind, metadata)
            )
            continue
        expected_mode = 0o600 | (entry.executable_bits or 0)
        if stat.S_IMODE(metadata.st_mode) != expected_mode:
            raise _StageUnavailable("staged file mode conflicts")
        source_digest = _digest_source_file(source_fd, entry, probe=probe)
        stage_digest = _digest_stage_file(
            stage_fd,
            entry.relative_path,
            expected_metadata=metadata,
            probe=probe,
        )
        if stage_digest != source_digest:
            raise _StageUnavailable("staged file bytes conflict")
        frozen_entries.append(
            _freeze_stage_entry(
                entry.relative_path,
                entry.kind,
                metadata,
                content_digest=stage_digest,
            )
        )
    root = os.fstat(stage_fd)
    if stat.S_IMODE(root.st_mode) != 0o700:
        raise _StageUnavailable("staged root mode conflicts")
    return _FrozenStageEvidence(
        root_device=root.st_dev,
        root_inode=root.st_ino,
        root_mtime_ns=root.st_mtime_ns,
        root_ctime_ns=root.st_ctime_ns,
        root_mode=stat.S_IMODE(root.st_mode),
        entries=tuple(frozen_entries),
    )


def _freeze_stage_entry(
    relative_path: PurePosixPath,
    kind: _EntryKind,
    metadata: os.stat_result,
    *,
    content_digest: bytes | None = None,
) -> _FrozenStageEntry:
    return _FrozenStageEntry(
        relative_path=relative_path,
        kind=kind,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size if kind is _EntryKind.REGULAR_FILE else None,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
        mode=stat.S_IMODE(metadata.st_mode),
        content_digest=content_digest,
    )


def _verify_frozen_stage_evidence(
    stage_fd: int,
    evidence: _FrozenStageEvidence,
    probe: LocalSkillCancellationProbe,
) -> None:
    _check_cancel(probe)
    try:
        root = os.fstat(stage_fd)
    except OSError as exc:
        raise _StageUnavailable("staged root metadata is unavailable") from exc
    if (
        root.st_dev,
        root.st_ino,
        root.st_mtime_ns,
        root.st_ctime_ns,
        stat.S_IMODE(root.st_mode),
    ) != (
        evidence.root_device,
        evidence.root_inode,
        evidence.root_mtime_ns,
        evidence.root_ctime_ns,
        evidence.root_mode,
    ):
        raise _StageUnavailable("staged root identity changed")
    actual = _observe_stage_membership(stage_fd, probe=probe)
    if tuple((path, kind) for path, kind, _metadata in actual) != tuple(
        (item.relative_path, item.kind) for item in evidence.entries
    ):
        raise _StageUnavailable("staged membership changed")
    for expected, (path, kind, metadata) in zip(evidence.entries, actual, strict=True):
        _check_cancel(probe)
        content_digest = None
        if kind is _EntryKind.REGULAR_FILE:
            content_digest = _digest_stage_file(
                stage_fd,
                path,
                expected_metadata=metadata,
                probe=probe,
            )
        current = _freeze_stage_entry(
            path,
            kind,
            metadata,
            content_digest=content_digest,
        )
        if current != expected:
            raise _StageUnavailable("staged entry identity or bytes changed")


def _observe_stage_membership(
    root_fd: int,
    *,
    probe: LocalSkillCancellationProbe | None = None,
) -> tuple[tuple[PurePosixPath, _EntryKind, os.stat_result], ...]:
    result: list[tuple[PurePosixPath, _EntryKind, os.stat_result]] = []
    operation_probe = probe or NeverCancelLocalSkillOperation()

    def walk(directory_fd: int, prefix: PurePosixPath) -> None:
        _check_cancel(operation_probe)
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise _StageUnavailable from exc
        for name in names:
            _check_cancel(operation_probe)
            relative = prefix / name if prefix.parts else PurePosixPath(name)
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise _StageUnavailable from exc
            if stat.S_ISDIR(metadata.st_mode):
                kind = _EntryKind.DIRECTORY
            elif stat.S_ISREG(metadata.st_mode):
                kind = _EntryKind.REGULAR_FILE
            else:
                raise _StageUnavailable("staged tree contains a non-ordinary entry")
            result.append((relative, kind, metadata))
            if kind is _EntryKind.DIRECTORY:
                try:
                    child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                except OSError as exc:
                    raise _StageUnavailable from exc
                try:
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)

    try:
        walk(root_fd, PurePosixPath())
    except RecursionError as exc:
        raise _StageUnavailable(
            "staged directory depth exhausted the host stack"
        ) from exc
    return tuple(result)


def _read_bounded_stage_file(
    root_fd: int,
    relative: PurePosixPath,
    *,
    maximum: int,
    probe: LocalSkillCancellationProbe | None = None,
) -> bytes:
    operation_probe = probe or NeverCancelLocalSkillOperation()
    parent_fd, name = _open_relative_parent(root_fd, relative)
    try:
        try:
            descriptor = os.open(name, _READ_FILE_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise _StageUnavailable from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise _StageUnavailable("staged entry is not a regular file")
            remaining = maximum + 1
            chunks: list[bytes] = []
            while remaining > 0:
                _check_cancel(operation_probe)
                amount = min(_COPY_CHUNK_BYTES, remaining)
                try:
                    chunk = os.read(descriptor, amount)
                except OSError as exc:
                    raise _StageUnavailable from exc
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _revalidate_source(
    source_fd: int,
    observation: _FrozenSourceObservation,
    *,
    probe: LocalSkillCancellationProbe | None = None,
) -> None:
    operation_probe = probe or NeverCancelLocalSkillOperation()
    _check_cancel(operation_probe)
    try:
        metadata = os.fstat(source_fd)
    except OSError as exc:
        raise _SourceUnavailable("held source metadata is unavailable") from exc
    if (metadata.st_dev, metadata.st_ino) != (
        observation.root_device,
        observation.root_inode,
    ):
        raise _SourceRaced("held source directory identity changed")
    try:
        rebound = open_absolute_directory_nofollow(observation.source_path)
    except FileNotFoundError as exc:
        raise _SourceRaced from exc
    except OSError as exc:
        raise _SourceUnavailable from exc
    try:
        try:
            rebound_stat = os.fstat(rebound)
        except OSError as exc:
            raise _SourceUnavailable("source path metadata is unavailable") from exc
        if (rebound_stat.st_dev, rebound_stat.st_ino) != (
            observation.root_device,
            observation.root_inode,
        ):
            raise _SourceRaced("source pathname binding changed")
    finally:
        os.close(rebound)
    actual = _observe_source_membership(source_fd, probe=operation_probe)
    if actual != observation.entries:
        raise _SourceRaced("source membership or identity changed")
    skill_bytes = _read_source_skill_document(
        source_fd,
        actual,
        maximum=MAX_SKILL_FILE_BYTES,
        probe=operation_probe,
    )
    if skill_bytes != observation.skill_document_bytes:
        raise _SourceRaced("source SKILL.md bytes changed")


def _arbitrate_stage_failure(
    source_fd: int,
    observation: _FrozenSourceObservation,
) -> LocalSkillInstallDisposition:
    try:
        _revalidate_source(source_fd, observation)
    except _SourceRaced:
        return LocalSkillInstallDisposition.SOURCE_RACED
    except _SourceUnavailable:
        return LocalSkillInstallDisposition.SOURCE_UNAVAILABLE
    return LocalSkillInstallDisposition.STAGING_UNAVAILABLE


def _arbitrate_allocation_failure(
    source_fd: int,
    observation: _FrozenSourceObservation,
) -> LocalSkillInstallDisposition:
    """Preserve source-first settlement after a process-local allocation failure."""

    try:
        _revalidate_source(source_fd, observation)
    except _SourceRaced:
        return LocalSkillInstallDisposition.SOURCE_RACED
    except _SourceUnavailable:
        return LocalSkillInstallDisposition.SOURCE_UNAVAILABLE
    except (MemoryError, RecursionError):
        # The initiating allocation failure remains the only proven owner when
        # the best-effort source fence itself cannot allocate.
        pass
    return LocalSkillInstallDisposition.STAGING_UNAVAILABLE


def _final_binding_cut(
    target: _TargetRoot,
    stage: _StageBinding,
    final_name: str,
    evidence: _FrozenStageEvidence,
    probe: LocalSkillCancellationProbe,
) -> None:
    try:
        rebound = open_absolute_directory_nofollow(target.path)
    except OSError as exc:
        raise _RootBindingRaced from exc
    try:
        try:
            metadata = os.fstat(rebound)
        except OSError as exc:
            raise _RootBindingRaced from exc
        if (metadata.st_dev, metadata.st_ino) != (target.device, target.inode):
            raise _RootBindingRaced("target root path binding changed")
    finally:
        os.close(rebound)
    try:
        stage_metadata = os.stat(
            stage.name, dir_fd=target.descriptor, follow_symlinks=False
        )
    except OSError as exc:
        raise _StageBindingRaced from exc
    if not stat.S_ISDIR(stage_metadata.st_mode) or (
        stage_metadata.st_dev,
        stage_metadata.st_ino,
    ) != (stage.device, stage.inode):
        raise _StageBindingRaced("private staging binding changed")
    try:
        destination_exists = _entry_exists(target.descriptor, final_name)
    except OSError as exc:
        raise _PublishIoUnavailable from exc
    if destination_exists:
        raise FileExistsError(final_name)
    _verify_frozen_stage_evidence(stage.descriptor, evidence, probe)


def _cleanup_or_wrap(
    target: _TargetRoot,
    stage: _StageBinding,
    prior: LocalSkillInstallOutcome,
) -> LocalSkillInstallOutcome:
    try:
        status = _cleanup_stage(target, stage)
    except (MemoryError, RecursionError):
        status = LocalSkillCleanupLocationStatus.KNOWN_AT_LAST_OBSERVATION
    if status is None:
        return prior
    return LocalSkillInstallOutcome(
        LocalSkillInstallDisposition.CLEANUP_UNAVAILABLE,
        prior.source_path,
        prior_disposition=prior.disposition,
        attempted_staging_path=stage.path,
        cleanup_location_status=status,
    )


def _cleanup_stage(
    target: _TargetRoot,
    stage: _StageBinding,
) -> LocalSkillCleanupLocationStatus | None:
    try:
        rebound = open_absolute_directory_nofollow(target.path)
    except OSError:
        return LocalSkillCleanupLocationStatus.UNRESOLVED_AFTER_NAMESPACE_INTERFERENCE
    try:
        root_stat = os.fstat(rebound)
        if (root_stat.st_dev, root_stat.st_ino) != (target.device, target.inode):
            return (
                LocalSkillCleanupLocationStatus.UNRESOLVED_AFTER_NAMESPACE_INTERFERENCE
            )
    finally:
        os.close(rebound)
    try:
        metadata = os.stat(stage.name, dir_fd=target.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except (OSError, RecursionError):
        return LocalSkillCleanupLocationStatus.KNOWN_AT_LAST_OBSERVATION
    if not stat.S_ISDIR(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != (
        stage.device,
        stage.inode,
    ):
        return LocalSkillCleanupLocationStatus.UNRESOLVED_AFTER_NAMESPACE_INTERFERENCE
    try:
        _remove_directory_contents(stage.descriptor)
        os.rmdir(stage.name, dir_fd=target.descriptor)
    except OSError:
        return LocalSkillCleanupLocationStatus.KNOWN_AT_LAST_OBSERVATION
    return None


def _remove_directory_contents(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                _remove_directory_contents(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _check_cancel(probe: LocalSkillCancellationProbe) -> None:
    if probe.cancellation_requested():
        raise _Cancelled


__all__ = [
    "AtomicLocalSkillPublisher",
    "ExclusiveDirectoryPublisher",
    "LocalSkillCancellationProbe",
    "LocalSkillCleanupLocationStatus",
    "LocalSkillInstallDisposition",
    "LocalSkillInstallOutcome",
    "LocalSkillInstallScope",
    "LocalSkillPublishUnavailableReason",
    "NeverCancelLocalSkillOperation",
    "PlatformExclusiveDirectoryPublisher",
]
