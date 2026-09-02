"""Immutable managed package publisher, instance state, locks, inspection, GC."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import errno
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from time import monotonic
from typing import Iterator
from uuid import uuid4

from pulsara_agent.capability.pulsara_home import PulsaraHomeResolution
from pulsara_agent.exclusive_publish import (
    ExclusivePublishPrimitiveUnavailable,
    PlatformExclusiveDirectoryPublisher,
)
from pulsara_agent.hooks.contracts import HookVisibilityScope
from pulsara_agent.local_source_binding import (
    DIRECTORY_NOFOLLOW_FLAGS,
    open_absolute_directory_nofollow,
    prepare_local_source_path,
)
from pulsara_agent.memory.scope import workspace_context_key
from pulsara_agent.plugins.contracts import (
    PLUGIN_INSTANCE_STATE_CONTRACT_ID,
    PluginCancellationPort,
    PluginCleanupLocationStatus,
    PluginDiagnostic,
    PluginDiagnosticCode,
    PluginGcDisposition,
    PluginGcLocationStatus,
    PluginGcOutcome,
    PluginGcProgress,
    PluginGcRef,
    PluginGcRefKind,
    PluginInstanceIdentity,
    PluginInstanceState,
    PluginScopeKind,
    PluginValidationSummary,
    PluginVersionInspection,
)
from pulsara_agent.plugins.package_core import (
    COPY_CHUNK_BYTES,
    FrozenPackageEntry,
    HeldPluginPackageObservation,
    PackageEntryKind,
    PluginPackageCancelled,
    PluginPackageInvalid,
    PluginPackageRaced,
    PluginPackageTimedOut,
    PluginPackageUnavailable,
    PluginSourceObserver,
    _entry_matches,
    _observe_membership,
    _open_regular_entry,
    revalidate_observation,
    tree_contains_secret,
)
from pulsara_agent.primitives.bounded_json import bounded_json_loads
from pulsara_agent.process_api_key_boundary import (
    ProcessApiKeyBoundary,
    ProcessApiKeyBoundaryCancelled,
    ProcessApiKeyBoundaryTimedOut,
    ProcessApiKeyScrubSet,
)

try:  # pragma: no cover - platform import branch
    import fcntl
except ImportError:  # pragma: no cover - unsupported platform
    fcntl = None  # type: ignore[assignment]


MAXIMUM_PLUGIN_STATE_BYTES = 1024 * 1024
_PACKAGE_ID = re.compile(r"pkg_[0-9a-f]{32}")
_PLUGIN_ID = re.compile(
    r"(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?"
)
_STAGE = re.compile(
    r"\.pulsara-stage-(pkg_[0-9a-f]{32})-([0-9a-f]{32})"
)
_STATE_TEMP = re.compile(
    r"\.pulsara-state-((?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)-([0-9a-f]{32})\.tmp"
)
_FILE_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


@dataclass(frozen=True, slots=True)
class PluginStoreLayout:
    home: Path
    identity: PluginInstanceIdentity

    @property
    def scope_parts(self) -> tuple[str, ...]:
        if self.identity.scope is PluginScopeKind.USER:
            return ("user",)
        assert self.identity.workspace_state_key is not None
        return ("workspace", self.identity.workspace_state_key)

    @property
    def plugin_package_parent(self) -> Path:
        return self.home.joinpath(
            "plugins", "packages", *self.scope_parts, self.identity.plugin_id
        )

    @property
    def state_parent(self) -> Path:
        return self.home.joinpath("plugins", "state", *self.scope_parts)

    @property
    def state_path(self) -> Path:
        return self.state_parent / f"{self.identity.plugin_id}.json"

    @property
    def data_root(self) -> Path:
        return self.home.joinpath(
            "plugins", "data", *self.scope_parts, self.identity.plugin_id
        )

    @property
    def instance_lock_path(self) -> Path:
        return self.home.joinpath(
            "plugins",
            "locks",
            "instances",
            *self.scope_parts,
            f"{self.identity.plugin_id}.lock",
        )

    def package_lock_path(self, package_install_id: str) -> Path:
        return self.home.joinpath(
            "plugins",
            "locks",
            "packages",
            *self.scope_parts,
            self.identity.plugin_id,
            f"{package_install_id}.lock",
        )


@dataclass(frozen=True, slots=True)
class StoreInstallResult:
    state: PluginInstanceState
    package_root: Path
    data_root: Path
    summary: PluginValidationSummary
    replaced: bool


@dataclass(frozen=True, slots=True)
class StoreAlreadyPresent:
    state: PluginInstanceState
    summary: PluginValidationSummary


@dataclass(frozen=True, slots=True)
class StoreStateCutUnknown:
    intended_state: PluginInstanceState
    last_known_cut: str


@dataclass(frozen=True, slots=True)
class StoreMutationFailure:
    diagnostic: PluginDiagnostic
    published_package_install_id: str | None = None


@dataclass(frozen=True, slots=True)
class StoreSourceInvalid:
    diagnostics: tuple[PluginDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class StoreOperationCancelled:
    pass


@dataclass(frozen=True, slots=True)
class StoreOperationTimedOut:
    pass


@dataclass(frozen=True, slots=True)
class StoreCleanupFailure:
    prior: object
    attempted_path: Path
    location_status: PluginCleanupLocationStatus
    diagnostic: PluginDiagnostic


StoreInstallOutcome = (
    StoreInstallResult
    | StoreAlreadyPresent
    | StoreStateCutUnknown
    | StoreMutationFailure
    | StoreSourceInvalid
    | StoreOperationCancelled
    | StoreOperationTimedOut
    | StoreCleanupFailure
)


class _StoreSettlementReady(RuntimeError):
    def __init__(self, outcome: StoreInstallOutcome, *, preserve_root: bool) -> None:
        self.outcome = outcome
        self.preserve_root = preserve_root
        super().__init__(type(outcome).__name__)


class _StageUnavailable(OSError):
    """Physical staged-tree I/O failed after source authority was frozen."""


@dataclass(frozen=True, slots=True)
class StoreEnablementResult:
    state: PluginInstanceState | None
    previous: PluginInstanceState | None
    stale_observed_install_id: str | None = None
    ack_unknown: bool = False
    diagnostic: PluginDiagnostic | None = None


@dataclass(frozen=True, slots=True)
class StoreRemovalResult:
    previous: PluginInstanceState | None
    removed: bool
    ack_unknown: bool = False
    diagnostic: PluginDiagnostic | None = None


@dataclass(frozen=True, slots=True)
class HeldPluginStateAggregate:
    states: tuple[PluginInstanceState, ...]
    _observations: tuple["_StateRootObservation", ...] = field(
        repr=False, compare=False
    )

    def revalidate(self) -> None:
        _revalidate_state_observations(self._observations)


class PhysicalLifetimeAnchor:
    """One independent shared package-lock open description."""

    __slots__ = ("_descriptor", "path", "_closed")

    def __init__(self, descriptor: int, path: Path) -> None:
        self._descriptor = descriptor
        self.path = path
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # flock is tied to the open-file description.  A duplicated anchor must
        # retain the lock after another duplicate closes, so close the fd
        # without an explicit LOCK_UN.
        os.close(self._descriptor)

    def duplicate(self) -> "PhysicalLifetimeAnchor":
        if self._closed:
            raise ValueError("cannot duplicate a closed physical lifetime anchor")
        return PhysicalLifetimeAnchor(os.dup(self._descriptor), self.path)

    def __del__(self) -> None:  # pragma: no cover - GC timing
        if not getattr(self, "_closed", True):
            try:
                self.close()
            except OSError:
                pass

    def __copy__(self) -> "PhysicalLifetimeAnchor":
        return self.duplicate()

    def __deepcopy__(self, memo: object) -> "PhysicalLifetimeAnchor":
        del memo
        return self.duplicate()


class ManagedPluginStore:
    def __init__(
        self,
        *,
        pulsara_home: PulsaraHomeResolution,
        api_key_boundary: ProcessApiKeyBoundary,
    ) -> None:
        if pulsara_home.path is None:
            raise ValueError("managed Plugin store requires a resolved home")
        self._home = prepare_local_source_path(pulsara_home.path)
        self._boundary = api_key_boundary
        self._observer = PluginSourceObserver(api_key_boundary)
        self._publisher = PlatformExclusiveDirectoryPublisher()

    @property
    def home(self) -> Path:
        return self._home

    def identity(
        self,
        *,
        scope: PluginScopeKind,
        plugin_id: str,
        workspace_root: Path | None,
    ) -> PluginInstanceIdentity:
        workspace_key = None
        if scope is PluginScopeKind.WORKSPACE:
            if workspace_root is None or not workspace_root.is_absolute():
                raise ValueError("workspace Plugin identity requires an absolute root")
            if not workspace_root.exists() or not workspace_root.is_dir():
                raise ValueError("workspace Plugin identity requires a project root")
            workspace_key = workspace_context_key(workspace_root.as_posix())
        return PluginInstanceIdentity(scope, plugin_id, workspace_key)

    def layout(self, identity: PluginInstanceIdentity) -> PluginStoreLayout:
        return PluginStoreLayout(self._home, identity)

    def install(
        self,
        observation: HeldPluginPackageObservation,
        *,
        scope: PluginScopeKind,
        workspace_root: Path | None,
        replace: bool,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort,
        scrub_set: ProcessApiKeyScrubSet,
    ) -> StoreInstallOutcome:
        identity = self.identity(
            scope=scope,
            plugin_id=observation.summary.manifest.name,
            workspace_root=workspace_root,
        )
        layout = self.layout(identity)
        _check_abort(deadline_monotonic, cancellation)
        try:
            with self._exclusive_lock(
                layout.instance_lock_path,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            ):
                current = self.read_state(layout)
                if current is not None and not replace:
                    summary = self.read_package_summary(
                        layout,
                        current.current_package_install_id,
                        deadline_monotonic=deadline_monotonic,
                        cancellation=cancellation,
                        scrub_set=scrub_set,
                    )
                    return StoreAlreadyPresent(current, summary)
                package_install_id = f"pkg_{uuid4().hex}"
                state = PluginInstanceState(
                    identity.plugin_id,
                    identity.scope,
                    package_install_id,
                    False,
                    identity.workspace_state_key,
                )
                package_lock = layout.package_lock_path(package_install_id)
                with self._exclusive_lock(
                    package_lock,
                    deadline_monotonic=deadline_monotonic,
                    cancellation=cancellation,
                ):
                    return self._publish_and_cut_state(
                        observation,
                        layout=layout,
                        state=state,
                        replacing=current is not None,
                        deadline_monotonic=deadline_monotonic,
                        cancellation=cancellation,
                        scrub_set=scrub_set,
                    )
        except PluginPackageCancelled:
            raise
        except PluginPackageTimedOut:
            raise
        except (MemoryError, OSError, ValueError):
            return StoreMutationFailure(
                _diagnostic(PluginDiagnosticCode.STATE_UNAVAILABLE)
            )

    def _publish_and_cut_state(
        self,
        observation: HeldPluginPackageObservation,
        *,
        layout: PluginStoreLayout,
        state: PluginInstanceState,
        replacing: bool,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort,
        scrub_set: ProcessApiKeyScrubSet,
    ) -> StoreInstallOutcome:
        try:
            package_parent_fd = _open_or_create_absolute_directory(
                layout.plugin_package_parent
            )
        except (MemoryError, OSError, ValueError):
            return StoreMutationFailure(
                _diagnostic(PluginDiagnosticCode.STAGING_UNAVAILABLE)
            )
        package_root = layout.plugin_package_parent / state.current_package_install_id
        stage_name = (
            f".pulsara-stage-{state.current_package_install_id}-{uuid4().hex}"
        )
        stage_path = layout.plugin_package_parent / stage_name
        stage_fd: int | None = None
        stage_identity: tuple[int, int] | None = None
        published = False
        phase = "stage"
        prior: object | None = None
        preserve_published_root = False
        try:
            _check_abort(deadline_monotonic, cancellation)
            os.mkdir(stage_name, 0o700, dir_fd=package_parent_fd)
            stage_fd = os.open(
                stage_name, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=package_parent_fd
            )
            stage_stat = os.fstat(stage_fd)
            stage_identity = (stage_stat.st_dev, stage_stat.st_ino)
            phase = "copy"
            _copy_observation_to_stage(
                observation,
                stage_fd,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            )
            revalidate_observation(
                observation,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            )
            phase = "stage"
            stage_observation = self._observer.observe(
                stage_path,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
                scrub_set=scrub_set,
                hook_package_install_id=state.current_package_install_id,
                hook_visibility=(
                    HookVisibilityScope.USER
                    if state.scope is PluginScopeKind.USER
                    else HookVisibilityScope.WORKSPACE
                ),
                hook_workspace_state_key=state.workspace_state_key,
            )
            try:
                if _summary_semantic(stage_observation.summary) != _summary_semantic(
                    observation.summary
                ):
                    raise OSError("staged Plugin semantics changed")
                phase = "paired"
                _verify_paired_bytes(
                    observation,
                    stage_observation,
                    deadline_monotonic=deadline_monotonic,
                    cancellation=cancellation,
                )
            finally:
                stage_observation.close()
            phase = "stage"
            _normalize_stage_modes(
                stage_fd,
                observation.entries,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            )
            normalized_entries = _observe_membership(
                stage_fd,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            )
            _verify_normalized_modes(stage_fd, normalized_entries)
            phase = "publish"
            while True:
                _check_abort(deadline_monotonic, cancellation)
                try:
                    with self._boundary.sync_guard(
                        deadline_monotonic=deadline_monotonic,
                        cancellation=cancellation,
                    ) as guard:
                        expected = guard.value
                        scrub_set.observe(expected)
                except ProcessApiKeyBoundaryCancelled as exc:
                    raise PluginPackageCancelled from exc
                except ProcessApiKeyBoundaryTimedOut as exc:
                    raise PluginPackageTimedOut from exc
                encoded_expected = os.fsencode(expected)
                contains = bool(expected) and (
                    encoded_expected in os.fsencode(package_root)
                    or tree_contains_secret(
                        stage_fd,
                        normalized_entries,
                        encoded_expected,
                        deadline_monotonic=deadline_monotonic,
                        cancellation=cancellation,
                    )
                )
                if contains:
                    raise PluginPackageInvalid(
                        (
                            _diagnostic(
                                PluginDiagnosticCode.SOURCE_CONTAINS_ACTIVE_API_KEY
                            ),
                        )
                    )
                try:
                    with self._boundary.sync_guard(
                        deadline_monotonic=deadline_monotonic,
                        cancellation=cancellation,
                    ) as guard:
                        scrub_set.observe(guard.value)
                        if guard.value != expected:
                            continue
                        if sys.platform == "darwin":
                            # Darwin's RENAME_EXCL rejects a 0500 source
                            # directory with EACCES.  The stage has already
                            # been normalized and verified at 0500; grant only
                            # the syscall-required owner-write bit while this
                            # exact held descriptor and API-key gate span the
                            # irreversible cut, then remove it immediately.
                            os.fchmod(stage_fd, 0o700)
                        try:
                            self._publisher.publish(
                                package_parent_fd,
                                stage_name,
                                state.current_package_install_id,
                            )
                            published = True
                        finally:
                            if sys.platform == "darwin":
                                os.fchmod(stage_fd, 0o500)
                        break
                except ProcessApiKeyBoundaryCancelled as exc:
                    raise PluginPackageCancelled from exc
                except ProcessApiKeyBoundaryTimedOut as exc:
                    raise PluginPackageTimedOut from exc
                except ExclusivePublishPrimitiveUnavailable as exc:
                    raise OSError("exclusive publish unavailable") from exc
                except OSError as exc:
                    if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                        raise FileExistsError(package_root) from exc
                    raise
            phase = "state"
            _check_abort(deadline_monotonic, cancellation)
            try:
                _write_state_atomic(
                    layout,
                    state,
                    replacing=replacing,
                    publisher=self._publisher,
                )
            except (MemoryError, OSError, ValueError):
                # A failed/interruptible rename admission is confirmed from the
                # exact state while the instance lock is still held.  If that
                # confirmation is itself unavailable the cut is genuinely
                # unknown and the published root must be preserved.
                try:
                    after_failed_cut = self.read_state(layout)
                except (OSError, ValueError):
                    preserve_published_root = True
                    prior = StoreStateCutUnknown(
                        state, "STATE_RENAME_ERROR_CONFIRMATION_UNAVAILABLE"
                    )
                else:
                    if after_failed_cut == state:
                        preserve_published_root = True
                        prior = StoreInstallResult(
                            state,
                            package_root,
                            layout.data_root,
                            observation.summary,
                            replacing,
                        )
                    else:
                        prior = StoreMutationFailure(
                            _diagnostic(PluginDiagnosticCode.STATE_UNAVAILABLE),
                            state.current_package_install_id,
                        )
                raise _StoreSettlementReady(
                    prior, preserve_root=preserve_published_root
                )
            try:
                confirmed = self.read_state(layout)
            except (OSError, ValueError):
                preserve_published_root = True
                prior = StoreStateCutUnknown(
                    state, "STATE_RENAME_FULL_CONFIRMATION_UNAVAILABLE"
                )
                raise _StoreSettlementReady(prior, preserve_root=True)
            if confirmed != state:
                preserve_published_root = True
                prior = StoreStateCutUnknown(
                    state, "STATE_RENAME_FULL_CONFIRMATION_CONFLICT"
                )
                raise _StoreSettlementReady(prior, preserve_root=True)
            preserve_published_root = True
            prior = StoreInstallResult(
                state,
                package_root,
                layout.data_root,
                observation.summary,
                replacing,
            )
            raise _StoreSettlementReady(prior, preserve_root=True)
        except _StoreSettlementReady as settled:
            prior = settled.outcome
            preserve_published_root = settled.preserve_root
        except PluginPackageCancelled:
            prior = StoreOperationCancelled()
        except PluginPackageTimedOut:
            prior = StoreOperationTimedOut()
        except PluginPackageInvalid as exc:
            if any(
                diagnostic.code
                is PluginDiagnosticCode.SOURCE_CONTAINS_ACTIVE_API_KEY
                for diagnostic in exc.diagnostics
            ):
                prior = StoreSourceInvalid(exc.diagnostics)
            else:
                prior = StoreMutationFailure(
                    _diagnostic(PluginDiagnosticCode.STAGING_UNAVAILABLE),
                    state.current_package_install_id if published else None,
                )
        except PluginPackageRaced:
            prior = StoreMutationFailure(
                _diagnostic(PluginDiagnosticCode.SOURCE_RACED),
                state.current_package_install_id if published else None,
            )
        except PluginPackageUnavailable as exc:
            prior = StoreMutationFailure(
                _diagnostic(
                    exc.code
                    if phase in {"copy", "paired"}
                    else PluginDiagnosticCode.STAGING_UNAVAILABLE
                ),
                state.current_package_install_id if published else None,
            )
        except _StageUnavailable:
            failure_code = PluginDiagnosticCode.STAGING_UNAVAILABLE
            try:
                # The source observation remains the higher-priority physical
                # owner even when stage I/O wins the first syscall race.
                revalidate_observation(
                    observation,
                    deadline_monotonic=deadline_monotonic,
                    cancellation=cancellation,
                )
            except PluginPackageRaced:
                failure_code = PluginDiagnosticCode.SOURCE_RACED
            except PluginPackageUnavailable:
                failure_code = PluginDiagnosticCode.SOURCE_UNAVAILABLE
            except (MemoryError, OSError, ValueError):
                failure_code = PluginDiagnosticCode.SOURCE_UNAVAILABLE
            except (PluginPackageCancelled, PluginPackageTimedOut):
                # A settled stage failure outranks a later outer abort.
                pass
            prior = StoreMutationFailure(
                _diagnostic(failure_code),
                state.current_package_install_id if published else None,
            )
        except FileExistsError:
            prior = StoreMutationFailure(
                _diagnostic(PluginDiagnosticCode.PUBLISH_CONFLICT),
                state.current_package_install_id if published else None,
            )
        except (MemoryError, OSError, ValueError):
            if prior is None:
                prior = StoreMutationFailure(
                    _diagnostic(
                        PluginDiagnosticCode.STATE_UNAVAILABLE
                        if phase == "state"
                        else (
                            PluginDiagnosticCode.SOURCE_UNAVAILABLE
                            if phase == "copy"
                            else PluginDiagnosticCode.STAGING_UNAVAILABLE
                        )
                    ),
                    state.current_package_install_id if published else None,
                )
        finally:
            if stage_fd is not None:
                os.close(stage_fd)
        assert prior is not None
        if preserve_published_root:
            os.close(package_parent_fd)
            return prior
        cleanup_name = state.current_package_install_id if published else stage_name
        cleanup_path = package_root if published else stage_path
        if stage_identity is None:
            os.close(package_parent_fd)
            return prior
        cleanup_failure = _cleanup_owned_tree(
            package_parent_fd,
            cleanup_name,
            expected_identity=stage_identity,
        )
        os.close(package_parent_fd)
        if cleanup_failure is None:
            return prior
        return StoreCleanupFailure(
            prior,
            cleanup_path,
            (
                PluginCleanupLocationStatus.UNREFERENCED_VERSION
                if published
                else PluginCleanupLocationStatus.STAGE_ONLY
            ),
            _diagnostic(PluginDiagnosticCode.CLEANUP_UNAVAILABLE, cleanup_path),
        )

    def read_state(self, layout: PluginStoreLayout) -> PluginInstanceState | None:
        try:
            parent = open_absolute_directory_nofollow(layout.state_parent)
        except FileNotFoundError:
            return None
        try:
            return _read_state_at(parent, layout)
        finally:
            os.close(parent)

    def observe_current_state_aggregate(
        self,
        *,
        workspace_root: Path | None,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort,
    ) -> HeldPluginStateAggregate:
        states, observations = self._observe_states(
            workspace_root=workspace_root,
            deadline_monotonic=deadline_monotonic,
            cancellation=cancellation,
        )
        return HeldPluginStateAggregate(states, observations)

    def set_enabled(
        self,
        identity: PluginInstanceIdentity,
        *,
        expected_package_install_id: str,
        enabled: bool,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort,
    ) -> StoreEnablementResult:
        layout = self.layout(identity)
        try:
            with self._exclusive_lock(
                layout.instance_lock_path,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            ):
                previous = self.read_state(layout)
                if previous is None:
                    return StoreEnablementResult(None, None)
                if previous.current_package_install_id != expected_package_install_id:
                    return StoreEnablementResult(
                        None,
                        previous,
                        stale_observed_install_id=previous.current_package_install_id,
                    )
                if previous.enabled == enabled:
                    return StoreEnablementResult(previous, previous)
                if enabled:
                    _check_abort(deadline_monotonic, cancellation)
                    try:
                        _ensure_data_root(layout.data_root)
                    except (MemoryError, OSError, ValueError):
                        return StoreEnablementResult(
                            None,
                            previous,
                            diagnostic=_diagnostic(
                                PluginDiagnosticCode.DATA_ROOT_UNAVAILABLE
                            ),
                        )
                updated = PluginInstanceState(
                    previous.plugin_id,
                    previous.scope,
                    previous.current_package_install_id,
                    enabled,
                    previous.workspace_state_key,
                )
                _check_abort(deadline_monotonic, cancellation)
                _write_state_atomic(layout, updated, replacing=True, publisher=self._publisher)
                try:
                    confirmed = self.read_state(layout)
                except (OSError, ValueError):
                    return StoreEnablementResult(
                        updated, previous, ack_unknown=True
                    )
                if confirmed != updated:
                    return StoreEnablementResult(
                        updated, previous, ack_unknown=True
                    )
                return StoreEnablementResult(updated, previous)
        except (PluginPackageCancelled, PluginPackageTimedOut):
            raise
        except (MemoryError, OSError, ValueError):
            return StoreEnablementResult(
                None,
                None,
                diagnostic=_diagnostic(PluginDiagnosticCode.STATE_UNAVAILABLE),
            )

    def ensure_instance_data_root(
        self,
        identity: PluginInstanceIdentity,
        *,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort,
    ) -> Path:
        _check_abort(deadline_monotonic, cancellation)
        layout = self.layout(identity)
        _ensure_data_root(layout.data_root)
        _check_abort(deadline_monotonic, cancellation)
        return layout.data_root

    def remove(
        self,
        identity: PluginInstanceIdentity,
        *,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort,
    ) -> StoreRemovalResult:
        layout = self.layout(identity)
        try:
            with self._exclusive_lock(
                layout.instance_lock_path,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            ):
                previous = self.read_state(layout)
                if previous is None:
                    return StoreRemovalResult(None, False)
                _check_abort(deadline_monotonic, cancellation)
                parent = open_absolute_directory_nofollow(layout.state_parent)
                try:
                    os.unlink(layout.state_path.name, dir_fd=parent)
                    try:
                        os.stat(
                            layout.state_path.name,
                            dir_fd=parent,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        return StoreRemovalResult(previous, True)
                    except OSError:
                        return StoreRemovalResult(previous, False, ack_unknown=True)
                    return StoreRemovalResult(previous, False, ack_unknown=True)
                finally:
                    os.close(parent)
        except (PluginPackageCancelled, PluginPackageTimedOut):
            raise
        except (MemoryError, OSError, ValueError):
            return StoreRemovalResult(
                None,
                False,
                diagnostic=_diagnostic(PluginDiagnosticCode.STATE_UNAVAILABLE),
            )

    def read_package_summary(
        self,
        layout: PluginStoreLayout,
        package_install_id: str,
        *,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort,
        scrub_set: ProcessApiKeyScrubSet,
    ) -> PluginValidationSummary:
        package_root = layout.plugin_package_parent / package_install_id
        anchor = self.acquire_package_anchor(
            layout,
            package_install_id,
            deadline_monotonic=deadline_monotonic,
            cancellation=cancellation,
        )
        try:
            observed = self._observer.observe(
                package_root,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
                scrub_set=scrub_set,
                hook_package_install_id=package_install_id,
                hook_visibility=(
                    HookVisibilityScope.USER
                    if layout.identity.scope is PluginScopeKind.USER
                    else HookVisibilityScope.WORKSPACE
                ),
                hook_workspace_state_key=layout.identity.workspace_state_key,
                hook_lifetime_anchor=anchor,
                scan_active_api_key=False,
                enforce_managed_admission=False,
            )
            try:
                if observed.summary.manifest.name != layout.identity.plugin_id:
                    raise ValueError("managed package manifest/state identity conflicts")
                return observed.summary
            finally:
                observed.close()
        finally:
            anchor.close()

    def acquire_package_anchor(
        self,
        layout: PluginStoreLayout,
        package_install_id: str,
        *,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort,
    ) -> PhysicalLifetimeAnchor:
        descriptor = _open_lock_file(layout.package_lock_path(package_install_id))
        if fcntl is None:
            os.close(descriptor)
            raise OSError("flock is unavailable")
        try:
            while True:
                _check_abort(deadline_monotonic, cancellation)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    # A shared physical consumer queues behind the exact package
                    # publisher without refreshing the caller's logical bound.
                    import time

                    time.sleep(
                        min(
                            0.01,
                            max(0.0, deadline_monotonic - monotonic()),
                        )
                    )
            # Abort may win alongside the successful syscall.  Do not publish a
            # lifetime anchor after its owner has already expired or cancelled.
            _check_abort(deadline_monotonic, cancellation)
        except BaseException:
            os.close(descriptor)
            raise
        return PhysicalLifetimeAnchor(descriptor, layout.package_lock_path(package_install_id))

    def package_in_use(
        self, layout: PluginStoreLayout, package_install_id: str
    ) -> bool:
        descriptor = _open_lock_file(layout.package_lock_path(package_install_id))
        try:
            if fcntl is None:
                return True
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        finally:
            os.close(descriptor)

    def _observe_states(
        self,
        *,
        workspace_root: Path | None,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort,
    ) -> tuple[tuple[PluginInstanceState, ...], tuple["_StateRootObservation", ...]]:
        targets = [(PluginScopeKind.USER, None)]
        if workspace_root is not None:
            targets.append(
                (PluginScopeKind.WORKSPACE, workspace_context_key(workspace_root.as_posix()))
            )
        states: list[PluginInstanceState] = []
        observations: list[_StateRootObservation] = []
        for scope, workspace_key in targets:
            _check_abort(deadline_monotonic, cancellation)
            identity = PluginInstanceIdentity(scope, "placeholder", workspace_key)
            # Only the scope path is used; the placeholder never becomes state.
            parent_path = self.layout(identity).state_parent
            try:
                parent = open_absolute_directory_nofollow(parent_path)
            except FileNotFoundError:
                observations.append(_StateRootObservation(parent_path, None, (), ()))
                continue
            root_stat = os.fstat(parent)
            names = _visible_state_names(parent)
            evidence: list[tuple[str, tuple[int, int, int, int, int]]] = []
            try:
                for name in names:
                    _check_abort(deadline_monotonic, cancellation)
                    if not name.endswith(".json"):
                        raise ValueError("visible Plugin state entry is malformed")
                    plugin_id = name.removesuffix(".json")
                    layout = self.layout(
                        PluginInstanceIdentity(scope, plugin_id, workspace_key)
                    )
                    state, file_evidence = _read_state_at_with_evidence(parent, layout)
                    if state is None:
                        raise OSError("Plugin state disappeared")
                    states.append(state)
                    evidence.append((name, file_evidence))
            finally:
                os.close(parent)
            observations.append(
                _StateRootObservation(
                    parent_path,
                    (root_stat.st_dev, root_stat.st_ino),
                    names,
                    tuple(evidence),
                )
            )
        ordered = tuple(sorted(states, key=_state_sort_key))
        if len({(item.scope, item.workspace_state_key, item.plugin_id) for item in ordered}) != len(ordered):
            raise ValueError("Plugin state identities are not unique")
        return ordered, tuple(observations)

    def _observe_versions(
        self,
        *,
        states: tuple[PluginInstanceState, ...],
        workspace_root: Path | None,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort,
    ) -> tuple[PluginVersionInspection, ...]:
        referenced = {
            (item.scope, item.workspace_state_key, item.plugin_id, item.current_package_install_id)
            for item in states
        }
        targets = [(PluginScopeKind.USER, None)]
        if workspace_root is not None:
            targets.append(
                (PluginScopeKind.WORKSPACE, workspace_context_key(workspace_root.as_posix()))
            )
        result: list[PluginVersionInspection] = []
        for scope, workspace_key in targets:
            identity = PluginInstanceIdentity(scope, "placeholder", workspace_key)
            scope_parent = self.layout(identity).plugin_package_parent.parent
            try:
                scope_fd = open_absolute_directory_nofollow(scope_parent)
            except FileNotFoundError:
                continue
            try:
                for plugin_id in sorted(os.listdir(scope_fd)):
                    _check_abort(deadline_monotonic, cancellation)
                    metadata = os.stat(plugin_id, dir_fd=scope_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(metadata.st_mode) or plugin_id.startswith("."):
                        continue
                    plugin_fd = os.open(plugin_id, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=scope_fd)
                    try:
                        for name in sorted(os.listdir(plugin_fd)):
                            if not _PACKAGE_ID.fullmatch(name):
                                continue
                            version_meta = os.stat(
                                name, dir_fd=plugin_fd, follow_symlinks=False
                            )
                            if not stat.S_ISDIR(version_meta.st_mode):
                                raise ValueError("managed Plugin version is not a directory")
                            item_identity = PluginInstanceIdentity(
                                scope, plugin_id, workspace_key
                            )
                            layout = self.layout(item_identity)
                            result.append(
                                PluginVersionInspection(
                                    item_identity,
                                    name,
                                    layout.plugin_package_parent / name,
                                    (scope, workspace_key, plugin_id, name) in referenced,
                                    self.package_in_use(layout, name),
                                )
                            )
                    finally:
                        os.close(plugin_fd)
            finally:
                os.close(scope_fd)
        return tuple(
            sorted(
                result,
                key=lambda item: (*_identity_sort_key(item.identity), item.package_install_id),
            )
        )

    def observe_version_inventory(
        self,
        *,
        states: tuple[PluginInstanceState, ...],
        workspace_root: Path | None,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort,
    ) -> tuple[PluginVersionInspection, ...]:
        """Expose one call-local version observation to PluginInspectionService."""

        return self._observe_versions(
            states=states,
            workspace_root=workspace_root,
            deadline_monotonic=deadline_monotonic,
            cancellation=cancellation,
        )

    def gc(
        self,
        *,
        workspace_root: Path | None,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort,
    ) -> PluginGcOutcome:
        removed: list[PluginGcRef] = []
        in_use: list[PluginGcRef] = []
        current: PluginGcRef | None = None
        try:
            states, observations = self._observe_states(
                workspace_root=workspace_root,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            )
            _revalidate_state_observations(observations)
            referenced = {
                (item.scope, item.workspace_state_key, item.plugin_id, item.current_package_install_id)
                for item in states
            }
            candidates = (
                *self._gc_package_candidates(
                    workspace_root=workspace_root,
                    deadline_monotonic=deadline_monotonic,
                    cancellation=cancellation,
                ),
                *self._gc_state_temp_candidates(
                    workspace_root=workspace_root,
                    deadline_monotonic=deadline_monotonic,
                    cancellation=cancellation,
                ),
            )
            candidates = tuple(sorted(candidates, key=lambda item: str(item[0].path)))
            for candidate, identity, package_id in candidates:
                _check_abort(deadline_monotonic, cancellation)
                if candidate.kind is PluginGcRefKind.VERSION and (
                    identity.scope,
                    identity.workspace_state_key,
                    identity.plugin_id,
                    package_id,
                ) in referenced:
                    continue
                current = candidate
                layout = self.layout(identity)
                lock_path = (
                    layout.instance_lock_path
                    if candidate.kind is PluginGcRefKind.STATE_TEMP
                    else layout.package_lock_path(package_id)
                )
                descriptor = _open_lock_file(lock_path)
                try:
                    if fcntl is None:
                        in_use.append(candidate)
                        current = None
                        continue
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        in_use.append(candidate)
                        current = None
                        continue
                    # The package/instance lock prevents a new owner cut while
                    # this candidate is removed.  Revalidate the complete
                    # state observation only after that exclusion is owned, so
                    # a publisher which completed between enumeration and this
                    # lock cannot turn a stale "unreferenced" fact into a
                    # deletion of the newly-current root.
                    _revalidate_state_observations(observations)
                    parent_path = (
                        layout.state_parent
                        if candidate.kind is PluginGcRefKind.STATE_TEMP
                        else layout.plugin_package_parent
                    )
                    parent = open_absolute_directory_nofollow(parent_path)
                    try:
                        metadata = os.stat(
                            candidate.path.name,
                            dir_fd=parent,
                            follow_symlinks=False,
                        )
                        if candidate.kind is PluginGcRefKind.STATE_TEMP:
                            if not stat.S_ISREG(metadata.st_mode):
                                raise OSError("Plugin state temp is not regular")
                            os.unlink(candidate.path.name, dir_fd=parent)
                        else:
                            if not stat.S_ISDIR(metadata.st_mode):
                                raise OSError("Plugin GC root is not a directory")
                            failure = _cleanup_owned_tree(
                                parent,
                                candidate.path.name,
                                expected_identity=(metadata.st_dev, metadata.st_ino),
                            )
                            if failure is not None:
                                raise OSError("Plugin GC deletion unavailable")
                    finally:
                        os.close(parent)
                    removed.append(candidate)
                    current = None
                finally:
                    os.close(descriptor)
            return PluginGcOutcome(
                PluginGcDisposition.COMPLETE,
                PluginGcProgress(
                    tuple(removed), tuple(in_use), None, None, False
                ),
            )
        except PluginPackageCancelled:
            return PluginGcOutcome(
                PluginGcDisposition.CANCELLED,
                PluginGcProgress(
                    tuple(removed),
                    tuple(in_use),
                    current,
                    _gc_location(current),
                    True,
                ),
            )
        except PluginPackageTimedOut:
            return PluginGcOutcome(
                PluginGcDisposition.TIMED_OUT,
                PluginGcProgress(
                    tuple(removed),
                    tuple(in_use),
                    current,
                    _gc_location(current),
                    True,
                ),
            )
        except (MemoryError, OSError, ValueError):
            return PluginGcOutcome(
                PluginGcDisposition.UNAVAILABLE,
                PluginGcProgress(
                    tuple(removed),
                    tuple(in_use),
                    current,
                    PluginGcLocationStatus.UNKNOWN if current is not None else None,
                    True,
                ),
                (_diagnostic(PluginDiagnosticCode.CLEANUP_UNAVAILABLE),),
            )

    def _gc_package_candidates(
        self,
        *,
        workspace_root: Path | None,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort,
    ) -> tuple[tuple[PluginGcRef, PluginInstanceIdentity, str], ...]:
        targets = [(PluginScopeKind.USER, None)]
        if workspace_root is not None:
            targets.append(
                (PluginScopeKind.WORKSPACE, workspace_context_key(workspace_root.as_posix()))
            )
        result: list[tuple[PluginGcRef, PluginInstanceIdentity, str]] = []
        for scope, workspace_key in targets:
            placeholder = PluginInstanceIdentity(scope, "placeholder", workspace_key)
            scope_parent = self.layout(placeholder).plugin_package_parent.parent
            try:
                scope_fd = open_absolute_directory_nofollow(scope_parent)
            except FileNotFoundError:
                continue
            try:
                for plugin_id in sorted(os.listdir(scope_fd)):
                    _check_abort(deadline_monotonic, cancellation)
                    metadata = os.stat(plugin_id, dir_fd=scope_fd, follow_symlinks=False)
                    if plugin_id.startswith("."):
                        continue
                    if not stat.S_ISDIR(metadata.st_mode) or not _PLUGIN_ID.fullmatch(
                        plugin_id
                    ):
                        raise ValueError("managed Plugin package instance is malformed")
                    identity = PluginInstanceIdentity(scope, plugin_id, workspace_key)
                    plugin_fd = os.open(
                        plugin_id, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=scope_fd
                    )
                    try:
                        for name in sorted(os.listdir(plugin_fd)):
                            package_id = None
                            kind = None
                            if _PACKAGE_ID.fullmatch(name):
                                package_id = name
                                kind = PluginGcRefKind.VERSION
                            elif (match := _STAGE.fullmatch(name)) is not None:
                                package_id = match.group(1)
                                kind = PluginGcRefKind.STAGE
                            if package_id is None or kind is None:
                                continue
                            result.append(
                                (
                                    PluginGcRef(
                                        kind,
                                        self.layout(identity).plugin_package_parent / name,
                                        package_id,
                                    ),
                                    identity,
                                    package_id,
                                )
                            )
                    finally:
                        os.close(plugin_fd)
            finally:
                os.close(scope_fd)
        return tuple(
            sorted(result, key=lambda item: str(item[0].path))
        )

    def _gc_state_temp_candidates(
        self,
        *,
        workspace_root: Path | None,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort,
    ) -> tuple[tuple[PluginGcRef, PluginInstanceIdentity, str], ...]:
        targets = [(PluginScopeKind.USER, None)]
        if workspace_root is not None:
            targets.append(
                (
                    PluginScopeKind.WORKSPACE,
                    workspace_context_key(workspace_root.as_posix()),
                )
            )
        result: list[tuple[PluginGcRef, PluginInstanceIdentity, str]] = []
        for scope, workspace_key in targets:
            placeholder = PluginInstanceIdentity(scope, "placeholder", workspace_key)
            parent_path = self.layout(placeholder).state_parent
            try:
                parent = open_absolute_directory_nofollow(parent_path)
            except FileNotFoundError:
                continue
            try:
                for name in sorted(os.listdir(parent)):
                    _check_abort(deadline_monotonic, cancellation)
                    match = _STATE_TEMP.fullmatch(name)
                    if match is None:
                        continue
                    plugin_id = match.group(1)
                    identity = PluginInstanceIdentity(scope, plugin_id, workspace_key)
                    result.append(
                        (
                            PluginGcRef(
                                PluginGcRefKind.STATE_TEMP,
                                self.layout(identity).state_parent / name,
                            ),
                            identity,
                            "",
                        )
                    )
            finally:
                os.close(parent)
        return tuple(result)

    @contextmanager
    def _exclusive_lock(
        self,
        path: Path,
        *,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort,
    ) -> Iterator[None]:
        descriptor = _open_lock_file(path)
        if fcntl is None:
            os.close(descriptor)
            raise OSError("flock is unavailable")
        try:
            while True:
                _check_abort(deadline_monotonic, cancellation)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    # Logical work queues behind the physical instance/package
                    # owner; no retry-count or lifetime cap is introduced.
                    import time

                    time.sleep(min(0.01, max(0.0, deadline_monotonic - monotonic())))
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


@dataclass(frozen=True, slots=True)
class _StateRootObservation:
    path: Path
    root_identity: tuple[int, int] | None
    names: tuple[str, ...]
    files: tuple[tuple[str, tuple[int, int, int, int, int]], ...]


def _open_or_create_absolute_directory(path: Path) -> int:
    if not path.is_absolute():
        raise ValueError("absolute directory path required")
    path = prepare_local_source_path(path)
    current = os.open(os.sep, DIRECTORY_NOFOLLOW_FLAGS)
    try:
        for component in path.parts[1:]:
            try:
                next_fd = os.open(
                    component, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=current
                )
            except FileNotFoundError:
                os.mkdir(component, 0o700, dir_fd=current)
                next_fd = os.open(
                    component, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=current
                )
                os.fchmod(next_fd, 0o700)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def _open_lock_file(path: Path) -> int:
    parent = _open_or_create_absolute_directory(path.parent)
    try:
        descriptor = os.open(
            path.name,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise OSError("Plugin lock is not a regular file")
        os.fchmod(descriptor, 0o600)
        return descriptor
    finally:
        os.close(parent)


def _copy_observation_to_stage(
    observation: HeldPluginPackageObservation,
    stage_fd: int,
    *,
    deadline_monotonic: float,
    cancellation: PluginCancellationPort,
) -> None:
    for entry in observation.entries:
        _check_abort(deadline_monotonic, cancellation)
        try:
            parent, name = _open_relative_parent(stage_fd, entry.relative_path)
        except OSError as exc:
            raise _StageUnavailable from exc
        try:
            if entry.kind is PackageEntryKind.DIRECTORY:
                try:
                    os.mkdir(name, 0o700, dir_fd=parent)
                    child = os.open(
                        name, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=parent
                    )
                    try:
                        os.fchmod(child, 0o700)
                    finally:
                        os.close(child)
                except OSError as exc:
                    raise _StageUnavailable from exc
                continue
            source = _open_regular_entry(observation.descriptor, entry)
            try:
                try:
                    destination = os.open(
                        name, _FILE_CREATE_FLAGS, 0o600, dir_fd=parent
                    )
                except OSError as exc:
                    raise _StageUnavailable from exc
                try:
                    try:
                        os.fchmod(
                            destination, 0o700 if entry.executable else 0o600
                        )
                    except OSError as exc:
                        raise _StageUnavailable from exc
                    while True:
                        _check_abort(deadline_monotonic, cancellation)
                        try:
                            chunk = os.read(source, COPY_CHUNK_BYTES)
                        except OSError as exc:
                            raise PluginPackageUnavailable(
                                PluginDiagnosticCode.SOURCE_UNAVAILABLE
                            ) from exc
                        if not chunk:
                            break
                        try:
                            _write_all(destination, chunk)
                        except OSError as exc:
                            raise _StageUnavailable from exc
                    try:
                        source_stat = os.fstat(source)
                    except OSError as exc:
                        raise PluginPackageUnavailable(
                            PluginDiagnosticCode.SOURCE_UNAVAILABLE
                        ) from exc
                    if not _entry_matches(entry, source_stat):
                        raise PluginPackageRaced
                finally:
                    os.close(destination)
            finally:
                os.close(source)
        finally:
            os.close(parent)


def _verify_paired_bytes(
    source: HeldPluginPackageObservation,
    stage: HeldPluginPackageObservation,
    *,
    deadline_monotonic: float,
    cancellation: PluginCancellationPort,
) -> None:
    source_shape = tuple((item.relative_path, item.kind, item.executable) for item in source.entries)
    stage_shape = tuple((item.relative_path, item.kind, item.executable) for item in stage.entries)
    if source_shape != stage_shape:
        raise OSError("staged Plugin shape changed")
    by_path = {item.relative_path: item for item in stage.entries}
    for source_entry in source.entries:
        if source_entry.kind is not PackageEntryKind.REGULAR_FILE:
            continue
        stage_entry = by_path[source_entry.relative_path]
        left = _open_regular_entry(source.descriptor, source_entry)
        try:
            right = _open_regular_entry(stage.descriptor, stage_entry)
        except (PluginPackageUnavailable, PluginPackageRaced, OSError) as exc:
            os.close(left)
            raise _StageUnavailable from exc
        try:
            while True:
                _check_abort(deadline_monotonic, cancellation)
                try:
                    source_chunk = os.read(left, COPY_CHUNK_BYTES)
                except OSError as exc:
                    raise PluginPackageUnavailable(
                        PluginDiagnosticCode.SOURCE_UNAVAILABLE
                    ) from exc
                try:
                    stage_chunk = os.read(right, COPY_CHUNK_BYTES)
                except OSError as exc:
                    raise _StageUnavailable from exc
                if source_chunk != stage_chunk:
                    raise _StageUnavailable
                if not source_chunk:
                    break
            try:
                source_stat = os.fstat(left)
            except OSError as exc:
                raise PluginPackageUnavailable(
                    PluginDiagnosticCode.SOURCE_UNAVAILABLE
                ) from exc
            if not _entry_matches(source_entry, source_stat):
                raise PluginPackageRaced
            try:
                stage_stat = os.fstat(right)
            except OSError as exc:
                raise _StageUnavailable from exc
            if not _entry_matches(stage_entry, stage_stat):
                raise _StageUnavailable
        finally:
            os.close(left)
            os.close(right)


def _normalize_stage_modes(
    stage_fd: int,
    entries: tuple[FrozenPackageEntry, ...],
    *,
    deadline_monotonic: float,
    cancellation: PluginCancellationPort,
) -> None:
    for entry in reversed(entries):
        _check_abort(deadline_monotonic, cancellation)
        parent, name = _open_relative_parent(stage_fd, entry.relative_path)
        try:
            if entry.kind is PackageEntryKind.DIRECTORY:
                descriptor = os.open(
                    name, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=parent
                )
                try:
                    os.fchmod(descriptor, 0o500)
                finally:
                    os.close(descriptor)
            else:
                descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=parent)
                try:
                    os.fchmod(descriptor, 0o500 if entry.executable else 0o400)
                finally:
                    os.close(descriptor)
        finally:
            os.close(parent)
    os.fchmod(stage_fd, 0o500)


def _verify_normalized_modes(
    stage_fd: int, entries: tuple[FrozenPackageEntry, ...]
) -> None:
    root_mode = stat.S_IMODE(os.fstat(stage_fd).st_mode)
    if root_mode != 0o500:
        raise _StageUnavailable("staged Plugin root mode is not normalized")
    for entry in entries:
        parent, name = _open_relative_parent(stage_fd, entry.relative_path)
        try:
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            raise _StageUnavailable from exc
        finally:
            os.close(parent)
        expected = (
            0o500
            if entry.kind is PackageEntryKind.DIRECTORY or entry.executable
            else 0o400
        )
        if stat.S_IMODE(metadata.st_mode) != expected:
            raise _StageUnavailable("staged Plugin member mode is not normalized")


def _open_relative_parent(root_fd: int, relative: PurePosixPath) -> tuple[int, str]:
    current = os.dup(root_fd)
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(
                component, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=current
            )
            os.close(current)
            current = next_fd
        return current, relative.parts[-1]
    except BaseException:
        os.close(current)
        raise


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        written = os.write(descriptor, view[offset:])
        if written <= 0:
            raise OSError("Plugin write made no progress")
        offset += written


def _summary_semantic(summary: PluginValidationSummary) -> object:
    return (
        summary.manifest,
        summary.skills.disposition,
        tuple(
            (skill.name, skill.description)
            for skill in summary.skills.skills
        ),
        summary.mcp.disposition,
        summary.mcp.mcp_servers,
        summary.hooks.disposition,
        summary.hooks.hook_definitions,
    )


def _ensure_data_root(path: Path) -> None:
    descriptor = _open_or_create_absolute_directory(path)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError("Plugin data root is not a directory")
        os.fchmod(descriptor, 0o700)
        confirmed = os.fstat(descriptor)
        if (
            confirmed.st_uid != os.geteuid()
            or stat.S_IMODE(confirmed.st_mode) != 0o700
        ):
            raise OSError("Plugin data root is not private and writable")
    finally:
        os.close(descriptor)


def _state_payload(state: PluginInstanceState) -> bytes:
    return json.dumps(
        {
            "contract_id": state.contract_id,
            "plugin_id": state.plugin_id,
            "scope": state.scope.value,
            "workspace_state_key": state.workspace_state_key,
            "current_package_install_id": state.current_package_install_id,
            "enabled": state.enabled,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _write_state_atomic(
    layout: PluginStoreLayout,
    state: PluginInstanceState,
    *,
    replacing: bool,
    publisher: PlatformExclusiveDirectoryPublisher,
) -> None:
    parent = _open_or_create_absolute_directory(layout.state_parent)
    temporary = f".pulsara-state-{state.plugin_id}-{uuid4().hex}.tmp"
    created = False
    try:
        descriptor = os.open(temporary, _FILE_CREATE_FLAGS, 0o600, dir_fd=parent)
        created = True
        try:
            payload = _state_payload(state)
            if len(payload) > MAXIMUM_PLUGIN_STATE_BYTES:
                raise ValueError("Plugin state exceeds its document bound")
            _write_all(descriptor, payload)
        finally:
            os.close(descriptor)
        if replacing:
            os.replace(
                temporary,
                layout.state_path.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
            )
        else:
            publisher.publish(parent, temporary, layout.state_path.name)
        created = False
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.close(parent)


def _read_state_at(
    parent: int, layout: PluginStoreLayout
) -> PluginInstanceState | None:
    result, _evidence = _read_state_at_with_evidence(parent, layout)
    return result


def _read_state_at_with_evidence(
    parent: int, layout: PluginStoreLayout
) -> tuple[PluginInstanceState | None, tuple[int, int, int, int, int]]:
    try:
        before = os.stat(
            layout.state_path.name, dir_fd=parent, follow_symlinks=False
        )
    except FileNotFoundError:
        return None, (0, 0, 0, 0, 0)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Plugin state is not a regular file")
    descriptor = os.open(layout.state_path.name, _FILE_READ_FLAGS, dir_fd=parent)
    try:
        opened = os.fstat(descriptor)
        expected = _file_identity(before)
        if _file_identity(opened) != expected:
            raise OSError("Plugin state binding raced")
        chunks: list[bytes] = []
        remaining = MAXIMUM_PLUGIN_STATE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(COPY_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if _file_identity(os.fstat(descriptor)) != expected:
            raise OSError("Plugin state changed during read")
    finally:
        os.close(descriptor)
    path_after = os.stat(
        layout.state_path.name, dir_fd=parent, follow_symlinks=False
    )
    if _file_identity(path_after) != expected:
        raise OSError("Plugin state pathname raced")
    raw = b"".join(chunks)
    value = bounded_json_loads(
        raw,
        maximum_bytes=MAXIMUM_PLUGIN_STATE_BYTES,
        maximum_nodes=64,
        maximum_depth=4,
        maximum_string_utf8_bytes=1024,
        reject_duplicate_keys=True,
    )
    if not isinstance(value, dict) or set(value) != {
        "contract_id",
        "plugin_id",
        "scope",
        "workspace_state_key",
        "current_package_install_id",
        "enabled",
    }:
        raise ValueError("Plugin state has an invalid shape")
    if value["contract_id"] != PLUGIN_INSTANCE_STATE_CONTRACT_ID or not isinstance(
        value["enabled"], bool
    ):
        raise ValueError("Plugin state contract is invalid")
    state = PluginInstanceState(
        value["plugin_id"],
        PluginScopeKind(value["scope"]),
        value["current_package_install_id"],
        value["enabled"],
        value["workspace_state_key"],
    )
    if (
        state.plugin_id != layout.identity.plugin_id
        or state.scope is not layout.identity.scope
        or state.workspace_state_key != layout.identity.workspace_state_key
    ):
        raise ValueError("Plugin state path identity conflicts")
    return state, expected


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _visible_state_names(descriptor: int) -> tuple[str, ...]:
    # Every hidden entry is outside current-state truth.  Only the exact
    # state-temp grammar is GC-owned; malformed/foreign dot entries remain
    # untouched and cannot poison ordinary enumeration.
    return tuple(
        sorted(name for name in os.listdir(descriptor) if not name.startswith("."))
    )


def _revalidate_state_observations(
    observations: tuple[_StateRootObservation, ...]
) -> None:
    for observation in observations:
        if observation.root_identity is None:
            try:
                appeared = open_absolute_directory_nofollow(observation.path)
            except FileNotFoundError:
                continue
            else:
                os.close(appeared)
                raise OSError("Plugin state root appeared during observation")
        rebound = open_absolute_directory_nofollow(observation.path)
        try:
            root = os.fstat(rebound)
            if (
                (root.st_dev, root.st_ino) != observation.root_identity
                or _visible_state_names(rebound) != observation.names
            ):
                raise OSError("Plugin state aggregate raced")
            for name, expected in observation.files:
                current = os.stat(name, dir_fd=rebound, follow_symlinks=False)
                if _file_identity(current) != expected:
                    raise OSError("Plugin state file raced")
        finally:
            os.close(rebound)


def _cleanup_owned_tree(
    parent_fd: int,
    name: str,
    *,
    expected_identity: tuple[int, int],
) -> PluginCleanupLocationStatus | None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except (MemoryError, OSError, ValueError):
        return PluginCleanupLocationStatus.UNKNOWN
    if not stat.S_ISDIR(metadata.st_mode) or (
        metadata.st_dev,
        metadata.st_ino,
    ) != expected_identity:
        return PluginCleanupLocationStatus.UNKNOWN
    try:
        root = os.open(name, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=parent_fd)
    except (MemoryError, OSError, ValueError):
        return PluginCleanupLocationStatus.UNKNOWN
    try:
        rebound = os.fstat(root)
        if (rebound.st_dev, rebound.st_ino) != expected_identity:
            return PluginCleanupLocationStatus.UNKNOWN
        os.fchmod(root, 0o700)
        files: list[PurePosixPath] = []
        directories: list[PurePosixPath] = []
        stack: list[tuple[int, PurePosixPath]] = [(os.dup(root), PurePosixPath())]
        try:
            while stack:
                descriptor, prefix = stack.pop()
                try:
                    for child_name in sorted(os.listdir(descriptor)):
                        relative = prefix / child_name if prefix.parts else PurePosixPath(child_name)
                        child_meta = os.stat(
                            child_name,
                            dir_fd=descriptor,
                            follow_symlinks=False,
                        )
                        if stat.S_ISDIR(child_meta.st_mode):
                            child = os.open(
                                child_name,
                                DIRECTORY_NOFOLLOW_FLAGS,
                                dir_fd=descriptor,
                            )
                            os.fchmod(child, 0o700)
                            directories.append(relative)
                            stack.append((child, relative))
                        else:
                            files.append(relative)
                finally:
                    os.close(descriptor)
        except BaseException:
            for descriptor, _prefix in stack:
                os.close(descriptor)
            raise
        for relative in files:
            relative_parent, child_name = _open_relative_parent(root, relative)
            try:
                os.unlink(child_name, dir_fd=relative_parent)
            finally:
                os.close(relative_parent)
        for relative in sorted(
            directories, key=lambda item: len(item.parts), reverse=True
        ):
            relative_parent, child_name = _open_relative_parent(root, relative)
            try:
                os.rmdir(child_name, dir_fd=relative_parent)
            finally:
                os.close(relative_parent)
    except (MemoryError, OSError, ValueError):
        return PluginCleanupLocationStatus.UNKNOWN
    finally:
        os.close(root)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except (MemoryError, OSError, ValueError):
        return PluginCleanupLocationStatus.UNKNOWN
    return None


def _state_sort_key(state: PluginInstanceState) -> tuple[str, str, str]:
    return (
        state.scope.value,
        state.workspace_state_key or "",
        state.plugin_id,
    )


def _identity_sort_key(identity: PluginInstanceIdentity) -> tuple[str, str, str]:
    return (
        identity.scope.value,
        identity.workspace_state_key or "",
        identity.plugin_id,
    )


def _gc_location(ref: PluginGcRef | None) -> PluginGcLocationStatus | None:
    if ref is None:
        return None
    if ref.kind is PluginGcRefKind.VERSION:
        return PluginGcLocationStatus.UNREFERENCED_VERSION
    if ref.kind is PluginGcRefKind.STAGE:
        return PluginGcLocationStatus.STAGE_ONLY
    return PluginGcLocationStatus.STATE_TEMP


def _check_abort(
    deadline_monotonic: float, cancellation: PluginCancellationPort
) -> None:
    if cancellation.cancellation_requested():
        raise PluginPackageCancelled
    if monotonic() >= deadline_monotonic:
        raise PluginPackageTimedOut


def _diagnostic(
    code: PluginDiagnosticCode, path: Path | None = None
) -> PluginDiagnostic:
    return PluginDiagnostic(
        code,
        code.value.replace("plugin_", "").replace("_", " "),
        None if path is None else str(path),
    )


__all__ = [
    "HeldPluginStateAggregate",
    "ManagedPluginStore",
    "PhysicalLifetimeAnchor",
    "PluginStoreLayout",
    "StoreAlreadyPresent",
    "StoreCleanupFailure",
    "StoreEnablementResult",
    "StoreInstallOutcome",
    "StoreInstallResult",
    "StoreMutationFailure",
    "StoreRemovalResult",
    "StoreStateCutUnknown",
]
