"""Typed application operations for loose local Skill management."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
import stat
from threading import Event

from pulsara_agent.capability.bundled_skills import (
    BundledSkillDefinitionProducer,
    BundledSkillDistributionBindingOwner,
)
from pulsara_agent.capability.contracts import LocalSkillRootKind
from pulsara_agent.capability.local_skill_removal import (
    LocalSkillRemovalIdentity,
    LocalSkillRemovalOutcome,
    observe_loose_skill_removal,
    remove_inspected_loose_skill,
)
from pulsara_agent.capability.user_skill_config import (
    USER_SKILL_CONFIG_NAME,
    workspace_skill_config_path,
)

from pulsara_agent.capability.local_skill_publisher import (
    AtomicLocalSkillPublisher,
    LocalSkillCancellationProbe,
    LocalSkillCleanupLocationStatus,
    LocalSkillInstallDisposition,
    LocalSkillInstallOutcome,
    LocalSkillInstallScope,
    LocalSkillPublishUnavailableReason,
)
from pulsara_agent.local_source_binding import (
    open_absolute_directory_nofollow,
    prepare_local_source_path,
)
from pulsara_agent.capability.local_skills import (
    MAX_SKILL_FILE_BYTES,
    SKILL_FILE_NAME,
    LooseSkillDefinitionProducer,
    ParsedSkillDocument,
    diagnostic_at,
    parse_skill_document,
)
from pulsara_agent.capability.pulsara_home import (
    PulsaraHomeResolution,
    UserHomeResolution,
    resolve_pulsara_home,
    resolve_user_home,
)
from pulsara_agent.capability.types import (
    SkillDiagnostic,
    SkillDiagnosticCode,
    SkillDiagnosticSeverity,
)
from pulsara_agent.capability.resolver import (
    EffectiveSkillCatalogInspection,
    SkillCatalogResolver,
)
from pulsara_agent.capability.plugin_skill_contracts import (
    FrozenPluginSkillDefinitions,
)


_READ_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


class LocalSkillValidationDisposition(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


class LocalSkillValidationUnavailableReason(StrEnum):
    SOURCE_MISSING = "SOURCE_MISSING"
    SOURCE_NOT_DIRECTORY = "SOURCE_NOT_DIRECTORY"
    SOURCE_DIRECTORY_UNAVAILABLE = "SOURCE_DIRECTORY_UNAVAILABLE"
    SKILL_DOCUMENT_READ_UNAVAILABLE = "SKILL_DOCUMENT_READ_UNAVAILABLE"
    SOURCE_RACED = "SOURCE_RACED"


@dataclass(frozen=True, slots=True)
class ValidateLocalSkillSourceRequest:
    source_path: Path

    def __post_init__(self) -> None:
        if not self.source_path.is_absolute():
            raise ValueError("Skill validation source must be lexical absolute")


@dataclass(frozen=True, slots=True)
class InstallLooseLocalSkillRequest:
    source_path: Path
    scope: LocalSkillInstallScope
    workspace_root: Path | None = None
    name: str | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, LocalSkillInstallScope):
            raise TypeError("loose Skill installation scope is not closed")
        if not self.source_path.is_absolute():
            raise ValueError("loose Skill installation source must be lexical absolute")
        if (self.scope is LocalSkillInstallScope.WORKSPACE) != (
            self.workspace_root is not None
        ):
            raise ValueError("loose Skill installation workspace conflicts with scope")
        if self.workspace_root is not None and not self.workspace_root.is_absolute():
            raise ValueError("loose Skill installation workspace must be resolved")


@dataclass(frozen=True, slots=True)
class InspectEffectiveSkillCatalogRequest:
    workspace_root: Path
    plugin_definitions: FrozenPluginSkillDefinitions

    def __post_init__(self) -> None:
        if not self.workspace_root.is_absolute():
            raise ValueError("Skill inspection workspace must be resolved and absolute")
        if not isinstance(self.plugin_definitions, FrozenPluginSkillDefinitions):
            raise TypeError("Skill inspection lacks its Plugin definition batch")


@dataclass(frozen=True, slots=True)
class LocalSkillValidationOutcome:
    disposition: LocalSkillValidationDisposition
    source_path: Path
    parsed: ParsedSkillDocument | None = None
    diagnostics: tuple[SkillDiagnostic, ...] = ()
    unavailable_reason: LocalSkillValidationUnavailableReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, LocalSkillValidationDisposition):
            raise TypeError("Skill validation disposition is not closed")
        if not self.source_path.is_absolute():
            raise ValueError("Skill validation source path is not absolute")
        if self.disposition is LocalSkillValidationDisposition.VALID:
            if self.parsed is None or self.unavailable_reason is not None:
                raise ValueError("valid Skill validation outcome is incomplete")
        elif self.disposition is LocalSkillValidationDisposition.INVALID:
            if (
                self.parsed is not None
                or not self.diagnostics
                or self.unavailable_reason is not None
            ):
                raise ValueError("invalid Skill validation outcome is incomplete")
        elif (
            self.parsed is not None
            or self.diagnostics
            or self.unavailable_reason is None
        ):
            raise ValueError("unavailable Skill validation outcome conflicts")


class EventLocalSkillCancellationProbe:
    """Call-local bridge shared by CLI SIGINT and future in-process adapters."""

    def __init__(self, event: Event | None = None) -> None:
        self._event = event or Event()

    def cancel(self) -> None:
        self._event.set()

    def cancellation_requested(self) -> bool:
        return self._event.is_set()


class LocalSkillManagementService:
    """CLI-independent owner of the three local Skill management operations."""

    def __init__(
        self,
        *,
        loose_producer: LooseSkillDefinitionProducer | None = None,
        publisher: AtomicLocalSkillPublisher | None = None,
        pulsara_home_resolution: PulsaraHomeResolution | None = None,
        user_home_resolution: UserHomeResolution | None = None,
        bundled_binding_owner: BundledSkillDistributionBindingOwner | None = None,
        catalog_resolver: SkillCatalogResolver | None = None,
    ) -> None:
        self._loose_producer = loose_producer
        self._publisher = publisher or AtomicLocalSkillPublisher()
        self._pulsara_home_resolution = pulsara_home_resolution
        self._user_home_resolution = user_home_resolution
        self._bundled_binding_owner = bundled_binding_owner
        self._catalog_resolver = catalog_resolver or SkillCatalogResolver()

    def validate_local_skill_source(
        self,
        request: ValidateLocalSkillSourceRequest,
    ) -> LocalSkillValidationOutcome:
        return _validate_source(request.source_path)

    def inspect_loose_skill_removal(
        self,
        *,
        skill_path: Path,
        scope: LocalSkillInstallScope,
        workspace_root: Path | None = None,
    ) -> LocalSkillRemovalIdentity:
        root, name, _ = self._removal_target(skill_path, scope, workspace_root)
        return observe_loose_skill_removal(root, name)

    def remove_loose_local_skill(
        self,
        *,
        skill_path: Path,
        scope: LocalSkillInstallScope,
        expected: LocalSkillRemovalIdentity,
        workspace_root: Path | None = None,
    ) -> LocalSkillRemovalOutcome:
        root, name, config = self._removal_target(skill_path, scope, workspace_root)
        return remove_inspected_loose_skill(
            root, name, expected=expected, config_path=config
        )

    def _removal_target(self, path, scope, workspace_root):
        if not isinstance(scope, LocalSkillInstallScope):
            raise TypeError("Skill removal scope is not closed")
        if (scope is LocalSkillInstallScope.WORKSPACE) != (workspace_root is not None):
            raise ValueError("Skill removal workspace conflicts with scope")
        if not path.is_absolute() or path.name != SKILL_FILE_NAME:
            raise ValueError("Skill removal requires the exact installed SKILL.md path")
        user_home = self._user_home_resolution or resolve_user_home()
        producer = self._loose_producer or LooseSkillDefinitionProducer(
            pulsara_home_resolution=self._home_resolution(
                user_home_resolution=user_home
            ),
            user_home_resolution=user_home,
        )
        policy = producer.prepare_root_policy(workspace_root or Path.cwd())
        allowed = (
            {LocalSkillRootKind.USER_PULSARA, LocalSkillRootKind.USER_AGENTS}
            if scope is LocalSkillInstallScope.USER
            else {LocalSkillRootKind.WORKSPACE_PULSARA}
        )
        roots = [
            root
            for root in policy.roots
            if root.root_kind in allowed and root.path == path.parent.parent
        ]
        if len(roots) != 1:
            raise ValueError("Skill is not an owned immediate child in this scope")
        if workspace_root is None:
            home = self._home_resolution(user_home_resolution=user_home)
            if home.path is None:
                raise ValueError("Pulsara home is unavailable")
            config = home.path / USER_SKILL_CONFIG_NAME
        else:
            config = workspace_skill_config_path(workspace_root)
        return roots[0].path, path.parent.name, config

    def install_loose_local_skill(
        self,
        request: InstallLooseLocalSkillRequest,
        *,
        cancellation: LocalSkillCancellationProbe | None = None,
    ) -> LocalSkillInstallOutcome:
        home = (
            self._home_resolution()
            if request.scope is LocalSkillInstallScope.USER
            else None
        )
        return self._publisher.install(
            source_path=request.source_path,
            scope=request.scope,
            workspace_root=request.workspace_root,
            pulsara_home=home,
            cancellation=cancellation,
            name=request.name,
            description=request.description,
        )

    def inspect_effective_skill_catalog(
        self,
        request: InspectEffectiveSkillCatalogRequest,
    ) -> EffectiveSkillCatalogInspection:
        producer = self._loose_producer
        if producer is None:
            user_home = self._user_home_resolution or resolve_user_home()
            producer = LooseSkillDefinitionProducer(
                pulsara_home_resolution=self._home_resolution(
                    user_home_resolution=user_home
                ),
                user_home_resolution=user_home,
            )
        policy = producer.prepare_root_policy(request.workspace_root)
        owner = self._bundled_binding_owner
        owns_binding = owner is None
        if owner is None:
            owner = BundledSkillDistributionBindingOwner()
        try:
            bundled = BundledSkillDefinitionProducer(owner).observe()
            loose = producer.observe(policy)
            return self._catalog_resolver.resolve(
                loose, request.plugin_definitions, bundled
            )
        finally:
            if owns_binding:
                owner.close()

    def _home_resolution(
        self, *, user_home_resolution: UserHomeResolution | None = None
    ) -> PulsaraHomeResolution:
        return self._pulsara_home_resolution or resolve_pulsara_home(
            user_home_resolution=user_home_resolution
        )


def _validate_source(source_path: Path) -> LocalSkillValidationOutcome:
    source = prepare_local_source_path(source_path)
    try:
        source_fd = open_absolute_directory_nofollow(source)
    except FileNotFoundError:
        return _validation_unavailable(
            source,
            LocalSkillValidationUnavailableReason.SOURCE_MISSING,
        )
    except NotADirectoryError:
        return _validation_unavailable(
            source,
            LocalSkillValidationUnavailableReason.SOURCE_NOT_DIRECTORY,
        )
    except OSError:
        try:
            metadata = source.lstat()
        except FileNotFoundError:
            reason = LocalSkillValidationUnavailableReason.SOURCE_MISSING
        except OSError:
            reason = LocalSkillValidationUnavailableReason.SOURCE_DIRECTORY_UNAVAILABLE
        else:
            reason = (
                LocalSkillValidationUnavailableReason.SOURCE_NOT_DIRECTORY
                if not stat.S_ISDIR(metadata.st_mode)
                else LocalSkillValidationUnavailableReason.SOURCE_DIRECTORY_UNAVAILABLE
            )
        return _validation_unavailable(source, reason)
    try:
        root_before = os.fstat(source_fd)
        try:
            names_before = tuple(sorted(os.listdir(source_fd)))
        except OSError:
            return _validation_unavailable(
                source,
                LocalSkillValidationUnavailableReason.SOURCE_DIRECTORY_UNAVAILABLE,
            )
        if SKILL_FILE_NAME not in names_before:
            if not _validation_source_stable(
                source, source_fd, root_before, names_before
            ):
                return _validation_unavailable(
                    source,
                    LocalSkillValidationUnavailableReason.SOURCE_RACED,
                )
            return LocalSkillValidationOutcome(
                LocalSkillValidationDisposition.INVALID,
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
            file_metadata = os.stat(
                SKILL_FILE_NAME,
                dir_fd=source_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(file_metadata.st_mode):
                raise OSError("SKILL.md is not a regular file")
            file_fd = os.open(SKILL_FILE_NAME, _READ_FILE_FLAGS, dir_fd=source_fd)
        except FileNotFoundError:
            return _validation_unavailable(
                source,
                LocalSkillValidationUnavailableReason.SOURCE_RACED,
            )
        except OSError:
            return _validation_unavailable(
                source,
                LocalSkillValidationUnavailableReason.SKILL_DOCUMENT_READ_UNAVAILABLE,
            )
        try:
            opened_before = os.fstat(file_fd)
            if _file_identity(opened_before) != _file_identity(file_metadata):
                return _validation_unavailable(
                    source,
                    LocalSkillValidationUnavailableReason.SOURCE_RACED,
                )
            chunks: list[bytes] = []
            remaining = MAX_SKILL_FILE_BYTES + 1
            while remaining:
                try:
                    chunk = os.read(file_fd, remaining)
                except OSError:
                    return _validation_unavailable(
                        source,
                        LocalSkillValidationUnavailableReason.SKILL_DOCUMENT_READ_UNAVAILABLE,
                    )
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            opened_after = os.fstat(file_fd)
            if _file_identity(opened_after) != _file_identity(opened_before):
                return _validation_unavailable(
                    source,
                    LocalSkillValidationUnavailableReason.SOURCE_RACED,
                )
        finally:
            os.close(file_fd)
        if not _validation_source_stable(source, source_fd, root_before, names_before):
            return _validation_unavailable(
                source,
                LocalSkillValidationUnavailableReason.SOURCE_RACED,
            )
        try:
            result = parse_skill_document(b"".join(chunks))
            diagnostics = tuple(
                diagnostic_at(item, source / SKILL_FILE_NAME)
                for item in result.diagnostics
            )
        except MemoryError:
            return _validation_unavailable(
                source,
                LocalSkillValidationUnavailableReason.SKILL_DOCUMENT_READ_UNAVAILABLE,
            )
        if result.parsed is None:
            return LocalSkillValidationOutcome(
                LocalSkillValidationDisposition.INVALID,
                source,
                diagnostics=diagnostics,
            )
        return LocalSkillValidationOutcome(
            LocalSkillValidationDisposition.VALID,
            source,
            parsed=result.parsed,
            diagnostics=diagnostics,
        )
    finally:
        os.close(source_fd)


def _validation_source_stable(
    source: Path,
    source_fd: int,
    root_before: os.stat_result,
    names_before: tuple[str, ...],
) -> bool:
    try:
        names_after = tuple(sorted(os.listdir(source_fd)))
        held_after = os.fstat(source_fd)
        rebound = open_absolute_directory_nofollow(source)
    except OSError:
        return False
    try:
        root_after = os.fstat(rebound)
        return (
            names_after == names_before
            and (held_after.st_mtime_ns, held_after.st_ctime_ns)
            == (root_before.st_mtime_ns, root_before.st_ctime_ns)
            and (root_after.st_dev, root_after.st_ino)
            == (root_before.st_dev, root_before.st_ino)
        )
    finally:
        os.close(rebound)


def _validation_unavailable(
    source: Path,
    reason: LocalSkillValidationUnavailableReason,
) -> LocalSkillValidationOutcome:
    return LocalSkillValidationOutcome(
        LocalSkillValidationDisposition.UNAVAILABLE,
        source,
        unavailable_reason=reason,
    )


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


__all__ = [
    "AtomicLocalSkillPublisher",
    "EventLocalSkillCancellationProbe",
    "InspectEffectiveSkillCatalogRequest",
    "InstallLooseLocalSkillRequest",
    "LocalSkillCleanupLocationStatus",
    "LocalSkillInstallDisposition",
    "LocalSkillInstallOutcome",
    "LocalSkillInstallScope",
    "LocalSkillManagementService",
    "LocalSkillPublishUnavailableReason",
    "LocalSkillValidationDisposition",
    "LocalSkillValidationOutcome",
    "LocalSkillValidationUnavailableReason",
    "ValidateLocalSkillSourceRequest",
]
