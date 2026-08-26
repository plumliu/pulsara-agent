"""CLI-independent typed management boundary for local Agent Plugins 1.0."""

from __future__ import annotations

import os
from pathlib import Path
from threading import Event

from pulsara_agent.capability.pulsara_home import (
    PulsaraHomeDisposition,
    PulsaraHomeResolution,
    resolve_pulsara_home,
)
from pulsara_agent.memory.scope import workspace_scope_key
from pulsara_agent.capability.types import (
    ConflictingSkillCandidateIssue,
    InvalidSkillCandidateIssue,
    ProducerUnavailableCause,
    ResolutionUnavailableCause,
    ShadowedSkillCandidateIssue,
    SkillDiagnostic,
    skill_origin_label,
)
from pulsara_agent.hooks.contracts import HookDiagnostic
from pulsara_agent.plugins.contracts import (
    AbortedPluginValidationOutcome,
    AlreadyPresentPluginInstallOutcome,
    CleanupUnavailablePluginInstallOutcome,
    FailedPluginEnablementOutcome,
    FailedPluginInstallOutcome,
    FailedPluginRemovalOutcome,
    GcLocalPluginPackagesRequest,
    InspectLocalPluginsRequest,
    InstallLocalPluginRequest,
    InvalidPluginValidationOutcome,
    PluginDiagnostic,
    PluginDiagnosticCode,
    PluginEnablementDisposition,
    PluginGcDisposition,
    PluginGcOutcome,
    PluginGcProgress,
    PluginGcRef,
    PluginInspectionAbort,
    PluginInspectionAbortReason,
    PluginInspectionDisposition,
    PluginInspectionOutcome,
    PluginInstallDisposition,
    PluginAuthor,
    PluginComponentSummary,
    PluginHookDefinitionSummary,
    PluginInstanceIdentity,
    PluginInstanceInspection,
    PluginManifest,
    PluginMcpHttpSummary,
    PluginMcpSseSummary,
    PluginMcpStdioSummary,
    PluginRemovalDisposition,
    PluginScopeKind,
    PluginSkillSummary,
    PluginValidationSummary,
    PluginVersionInspection,
    PluginValidationDisposition,
    RemoveLocalPluginRequest,
    RemovedPluginOutcome,
    SetLocalPluginEnabledRequest,
    SettledPluginEnablementOutcome,
    SuccessfulPluginInstallOutcome,
    UnavailablePluginValidationOutcome,
    ValidateLocalPluginSourceRequest,
    ValidPluginValidationOutcome,
)
from pulsara_agent.plugins.package_core import (
    PluginPackageCancelled,
    PluginPackageInvalid,
    PluginPackageRaced,
    PluginPackageTimedOut,
    PluginPackageUnavailable,
    PluginSourceObserver,
)
from pulsara_agent.plugins.inspection import PluginInspectionService
from pulsara_agent.plugins.package_store import (
    ManagedPluginStore,
    StoreAlreadyPresent,
    StoreCleanupFailure,
    StoreInstallOutcome,
    StoreInstallResult,
    StoreMutationFailure,
    StoreOperationCancelled,
    StoreOperationTimedOut,
    StoreSourceInvalid,
    StoreStateCutUnknown,
)
from pulsara_agent.process_api_key_boundary import (
    ProcessApiKeyBoundary,
    ProcessApiKeyBoundaryCancelled,
    ProcessApiKeyBoundaryTimedOut,
    ProcessApiKeyScrubSet,
)


class PluginManagementService:
    """The sole six-operation in-process Plugin management service."""

    def __init__(
        self,
        *,
        api_key_boundary: ProcessApiKeyBoundary,
        pulsara_home_resolution: PulsaraHomeResolution | None = None,
    ) -> None:
        self._api_key_boundary = api_key_boundary
        self._home_resolution = pulsara_home_resolution

    def validate_local_plugin_source(
        self, request: ValidateLocalPluginSourceRequest
    ):
        try:
            scrub_set = self._capture_scrub_set(request)
        except ProcessApiKeyBoundaryCancelled:
            return AbortedPluginValidationOutcome(
                _withheld_source_path(), PluginValidationDisposition.CANCELLED
            )
        except ProcessApiKeyBoundaryTimedOut:
            return AbortedPluginValidationOutcome(
                _withheld_source_path(), PluginValidationDisposition.TIMED_OUT
            )
        observer = PluginSourceObserver(self._api_key_boundary)
        try:
            observation = observer.observe(
                request.source_path,
                deadline_monotonic=request.deadline_monotonic,
                cancellation=request.cancellation,
                scrub_set=scrub_set,
            )
        except PluginPackageInvalid as exc:
            return InvalidPluginValidationOutcome(
                _safe_path(request.source_path, scrub_set),
                _safe_diagnostics(exc.diagnostics, scrub_set),
            )
        except PluginPackageUnavailable as exc:
            return UnavailablePluginValidationOutcome(
                _safe_path(request.source_path, scrub_set),
                (_diagnostic(exc.code),),
            )
        except PluginPackageRaced:
            return UnavailablePluginValidationOutcome(
                _safe_path(request.source_path, scrub_set),
                (_diagnostic(PluginDiagnosticCode.SOURCE_RACED),),
            )
        except PluginPackageCancelled:
            return AbortedPluginValidationOutcome(
                _safe_path(request.source_path, scrub_set),
                PluginValidationDisposition.CANCELLED,
            )
        except PluginPackageTimedOut:
            return AbortedPluginValidationOutcome(
                _safe_path(request.source_path, scrub_set),
                PluginValidationDisposition.TIMED_OUT,
            )
        except (MemoryError, OSError, ValueError):
            return UnavailablePluginValidationOutcome(
                _safe_path(request.source_path, scrub_set),
                (_diagnostic(PluginDiagnosticCode.SOURCE_UNAVAILABLE),),
            )
        try:
            return ValidPluginValidationOutcome(
                _safe_path(request.source_path, scrub_set),
                _safe_summary(observation.summary, scrub_set),
                _safe_diagnostics(observation.diagnostics, scrub_set),
            )
        finally:
            observation.close()

    def install_local_plugin(self, request: InstallLocalPluginRequest):
        try:
            scrub_set = self._capture_scrub_set(request)
        except ProcessApiKeyBoundaryCancelled:
            return FailedPluginInstallOutcome(
                PluginInstallDisposition.CANCELLED, _withheld_source_path()
            )
        except ProcessApiKeyBoundaryTimedOut:
            return FailedPluginInstallOutcome(
                PluginInstallDisposition.TIMED_OUT, _withheld_source_path()
            )
        safe_source_path = _safe_path(request.source_path, scrub_set)
        store = self._store()
        if store is None:
            return FailedPluginInstallOutcome(
                PluginInstallDisposition.UNAVAILABLE,
                safe_source_path,
                diagnostics=(
                    _diagnostic(PluginDiagnosticCode.HOME_CONFIGURATION_INVALID),
                ),
            )
        workspace_diagnostic = _workspace_diagnostic(
            request.scope, request.workspace_root
        )
        if workspace_diagnostic is not None:
            return FailedPluginInstallOutcome(
                PluginInstallDisposition.UNAVAILABLE,
                safe_source_path,
                diagnostics=(workspace_diagnostic,),
            )
        observer = PluginSourceObserver(self._api_key_boundary)
        try:
            observation = observer.observe(
                request.source_path,
                deadline_monotonic=request.deadline_monotonic,
                cancellation=request.cancellation,
                scrub_set=scrub_set,
            )
        except PluginPackageInvalid as exc:
            return FailedPluginInstallOutcome(
                PluginInstallDisposition.INVALID,
                safe_source_path,
                diagnostics=_safe_diagnostics(exc.diagnostics, scrub_set),
            )
        except PluginPackageUnavailable as exc:
            return FailedPluginInstallOutcome(
                PluginInstallDisposition.UNAVAILABLE,
                safe_source_path,
                diagnostics=(_diagnostic(exc.code),),
            )
        except PluginPackageRaced:
            return FailedPluginInstallOutcome(
                PluginInstallDisposition.UNAVAILABLE,
                safe_source_path,
                diagnostics=(_diagnostic(PluginDiagnosticCode.SOURCE_RACED),),
            )
        except PluginPackageCancelled:
            return FailedPluginInstallOutcome(
                PluginInstallDisposition.CANCELLED, safe_source_path
            )
        except PluginPackageTimedOut:
            return FailedPluginInstallOutcome(
                PluginInstallDisposition.TIMED_OUT, safe_source_path
            )
        except (MemoryError, OSError, ValueError):
            return FailedPluginInstallOutcome(
                PluginInstallDisposition.UNAVAILABLE,
                safe_source_path,
                diagnostics=(
                    _diagnostic(PluginDiagnosticCode.SOURCE_UNAVAILABLE),
                ),
            )
        try:
            identity = store.identity(
                scope=request.scope,
                plugin_id=observation.summary.manifest.name,
                workspace_root=request.workspace_root,
            )
            try:
                result = store.install(
                    observation,
                    scope=request.scope,
                    workspace_root=request.workspace_root,
                    replace=request.replace,
                    deadline_monotonic=request.deadline_monotonic,
                    cancellation=request.cancellation,
                    scrub_set=scrub_set,
                )
            except PluginPackageCancelled:
                result = StoreOperationCancelled()
            except PluginPackageTimedOut:
                result = StoreOperationTimedOut()
            return _install_outcome(
                result,
                source_path=safe_source_path,
                identity=_safe_identity(identity, scrub_set),
                diagnostics=_safe_diagnostics(observation.diagnostics, scrub_set),
                scrub_set=scrub_set,
            )
        finally:
            observation.close()

    def set_local_plugin_enabled(self, request: SetLocalPluginEnabledRequest):
        identity = _request_identity(
            request.scope, request.plugin_id, request.workspace_root
        )
        try:
            scrub_set = self._capture_scrub_set(request)
        except ProcessApiKeyBoundaryCancelled:
            return _enablement_failure(
                request,
                _withheld_identity(request.scope),
                PluginEnablementDisposition.CANCELLED,
            )
        except ProcessApiKeyBoundaryTimedOut:
            return _enablement_failure(
                request,
                _withheld_identity(request.scope),
                PluginEnablementDisposition.TIMED_OUT,
            )

        def settle(value):
            return _safe_enablement_outcome(value, scrub_set)

        store = self._store()
        if store is None:
            return settle(
                _enablement_failure(
                    request,
                    identity,
                    PluginEnablementDisposition.UNAVAILABLE,
                    PluginDiagnosticCode.HOME_CONFIGURATION_INVALID,
                )
            )
        workspace_diagnostic = _workspace_diagnostic(
            request.scope, request.workspace_root
        )
        if workspace_diagnostic is not None:
            return settle(
                FailedPluginEnablementOutcome(
                    PluginEnablementDisposition.UNAVAILABLE,
                    identity,
                    request.enabled,
                    diagnostics=(workspace_diagnostic,),
                )
            )
        try:
            result = store.set_enabled(
                identity,
                expected_package_install_id=(
                    request.expected_current_package_install_id
                ),
                enabled=request.enabled,
                deadline_monotonic=request.deadline_monotonic,
                cancellation=request.cancellation,
            )
        except PluginPackageCancelled:
            return settle(
                _enablement_failure(
                    request, identity, PluginEnablementDisposition.CANCELLED
                )
            )
        except PluginPackageTimedOut:
            return settle(
                _enablement_failure(
                    request, identity, PluginEnablementDisposition.TIMED_OUT
                )
            )
        if result.diagnostic is not None:
            return settle(
                FailedPluginEnablementOutcome(
                    PluginEnablementDisposition.UNAVAILABLE,
                    identity,
                    request.enabled,
                    diagnostics=(result.diagnostic,),
                )
            )
        if result.stale_observed_install_id is not None:
            return settle(
                FailedPluginEnablementOutcome(
                    PluginEnablementDisposition.STALE,
                    identity,
                    request.enabled,
                    request.expected_current_package_install_id,
                    result.stale_observed_install_id,
                    diagnostics=(_diagnostic(PluginDiagnosticCode.STATE_RACED),),
                )
            )
        if result.previous is None:
            return settle(
                FailedPluginEnablementOutcome(
                    PluginEnablementDisposition.NOT_FOUND,
                    identity,
                    request.enabled,
                    diagnostics=(
                        _diagnostic(PluginDiagnosticCode.PACKAGE_NOT_FOUND),
                    ),
                )
            )
        if result.ack_unknown:
            return settle(
                FailedPluginEnablementOutcome(
                    PluginEnablementDisposition.ACK_UNKNOWN,
                    identity,
                    request.enabled,
                    request.expected_current_package_install_id,
                    result.previous.current_package_install_id,
                    "STATE_RENAME_FULL_CONFIRMATION_UNAVAILABLE",
                )
            )
        assert result.state is not None
        unchanged = result.previous.enabled == request.enabled
        if request.enabled:
            disposition = (
                PluginEnablementDisposition.ALREADY_ENABLED
                if unchanged
                else PluginEnablementDisposition.ENABLED
            )
        else:
            disposition = (
                PluginEnablementDisposition.ALREADY_DISABLED
                if unchanged
                else PluginEnablementDisposition.DISABLED
            )
        return settle(
            SettledPluginEnablementOutcome(
                disposition,
                identity,
                result.state.current_package_install_id,
                result.state.enabled,
            )
        )

    def remove_local_plugin(self, request: RemoveLocalPluginRequest):
        identity = _request_identity(
            request.scope, request.plugin_id, request.workspace_root
        )
        try:
            scrub_set = self._capture_scrub_set(request)
        except ProcessApiKeyBoundaryCancelled:
            return FailedPluginRemovalOutcome(
                PluginRemovalDisposition.CANCELLED,
                _withheld_identity(request.scope),
            )
        except ProcessApiKeyBoundaryTimedOut:
            return FailedPluginRemovalOutcome(
                PluginRemovalDisposition.TIMED_OUT,
                _withheld_identity(request.scope),
            )

        def settle(value):
            return _safe_removal_outcome(value, scrub_set)

        store = self._store()
        if store is None:
            return settle(
                FailedPluginRemovalOutcome(
                    PluginRemovalDisposition.UNAVAILABLE,
                    identity,
                    diagnostics=(
                        _diagnostic(PluginDiagnosticCode.HOME_CONFIGURATION_INVALID),
                    ),
                )
            )
        workspace_diagnostic = _workspace_diagnostic(
            request.scope, request.workspace_root
        )
        if workspace_diagnostic is not None:
            return settle(
                FailedPluginRemovalOutcome(
                    PluginRemovalDisposition.UNAVAILABLE,
                    identity,
                    diagnostics=(workspace_diagnostic,),
                )
            )
        try:
            result = store.remove(
                identity,
                deadline_monotonic=request.deadline_monotonic,
                cancellation=request.cancellation,
            )
        except PluginPackageCancelled:
            return settle(
                FailedPluginRemovalOutcome(
                    PluginRemovalDisposition.CANCELLED, identity
                )
            )
        except PluginPackageTimedOut:
            return settle(
                FailedPluginRemovalOutcome(
                    PluginRemovalDisposition.TIMED_OUT, identity
                )
            )
        if result.diagnostic is not None:
            return settle(
                FailedPluginRemovalOutcome(
                    PluginRemovalDisposition.UNAVAILABLE,
                    identity,
                    diagnostics=(result.diagnostic,),
                )
            )
        if result.previous is None:
            return settle(
                FailedPluginRemovalOutcome(
                    PluginRemovalDisposition.NOT_FOUND,
                    identity,
                    diagnostics=(
                        _diagnostic(PluginDiagnosticCode.PACKAGE_NOT_FOUND),
                    ),
                )
            )
        if result.ack_unknown:
            return settle(
                FailedPluginRemovalOutcome(
                    PluginRemovalDisposition.ACK_UNKNOWN,
                    identity,
                    result.previous.current_package_install_id,
                    "STATE_UNLINK_FULL_CONFIRMATION_UNAVAILABLE",
                )
            )
        if result.removed:
            return settle(
                RemovedPluginOutcome(
                    identity, result.previous.current_package_install_id
                )
            )
        return settle(
            FailedPluginRemovalOutcome(
                PluginRemovalDisposition.UNAVAILABLE,
                identity,
                result.previous.current_package_install_id,
                diagnostics=(_diagnostic(PluginDiagnosticCode.STATE_UNAVAILABLE),),
            )
        )

    def inspect_local_plugins(self, request: InspectLocalPluginsRequest):
        try:
            scrub_set = self._capture_scrub_set(request)
        except ProcessApiKeyBoundaryCancelled:
            return PluginInspectionAbort(PluginInspectionAbortReason.CANCELLED)
        except ProcessApiKeyBoundaryTimedOut:
            return PluginInspectionAbort(PluginInspectionAbortReason.TIMED_OUT)
        resolution = self._home_resolution or resolve_pulsara_home()
        if resolution.disposition is not PulsaraHomeDisposition.RESOLVED:
            return PluginInspectionOutcome(
                PluginInspectionDisposition.UNAVAILABLE,
                (),
                (),
                (_diagnostic(PluginDiagnosticCode.HOME_CONFIGURATION_INVALID),),
            )
        if request.workspace_root is not None and not _workspace_is_available(
            request.workspace_root
        ):
            return PluginInspectionOutcome(
                PluginInspectionDisposition.UNAVAILABLE,
                (),
                (),
                (_diagnostic(PluginDiagnosticCode.WORKSPACE_REQUIRED),),
            )
        store = ManagedPluginStore(
            pulsara_home=resolution,
            api_key_boundary=self._api_key_boundary,
        )
        try:
            outcome = PluginInspectionService(
                store=store,
                api_key_boundary=self._api_key_boundary,
                pulsara_home_resolution=resolution,
            ).inspect(
                workspace_root=request.workspace_root,
                deadline_monotonic=request.deadline_monotonic,
                cancellation=request.cancellation,
                scrub_set=scrub_set,
            )
            return _safe_inspection_outcome(outcome, scrub_set)
        except PluginPackageCancelled:
            return PluginInspectionAbort(PluginInspectionAbortReason.CANCELLED)
        except PluginPackageTimedOut:
            return PluginInspectionAbort(PluginInspectionAbortReason.TIMED_OUT)

    def gc_local_plugin_packages(self, request: GcLocalPluginPackagesRequest):
        try:
            scrub_set = self._capture_scrub_set(request)
        except ProcessApiKeyBoundaryCancelled:
            return PluginGcOutcome(
                PluginGcDisposition.CANCELLED,
                PluginGcProgress(unvisited_suffix=True),
            )
        except ProcessApiKeyBoundaryTimedOut:
            return PluginGcOutcome(
                PluginGcDisposition.TIMED_OUT,
                PluginGcProgress(unvisited_suffix=True),
            )
        store = self._store()
        if store is None:
            return PluginGcOutcome(
                PluginGcDisposition.UNAVAILABLE,
                PluginGcProgress(unvisited_suffix=True),
                (_diagnostic(PluginDiagnosticCode.HOME_CONFIGURATION_INVALID),),
            )
        if request.workspace_root is not None and not _workspace_is_available(
            request.workspace_root
        ):
            return PluginGcOutcome(
                PluginGcDisposition.UNAVAILABLE,
                PluginGcProgress(unvisited_suffix=True),
                (_diagnostic(PluginDiagnosticCode.WORKSPACE_REQUIRED),),
            )
        return _safe_gc_outcome(
            store.gc(
            workspace_root=request.workspace_root,
            deadline_monotonic=request.deadline_monotonic,
            cancellation=request.cancellation,
            ),
            scrub_set,
        )

    def _store(self) -> ManagedPluginStore | None:
        resolution = self._home_resolution or resolve_pulsara_home()
        if resolution.disposition is not PulsaraHomeDisposition.RESOLVED:
            return None
        return ManagedPluginStore(
            pulsara_home=resolution,
            api_key_boundary=self._api_key_boundary,
        )

    def _capture_scrub_set(self, request) -> ProcessApiKeyScrubSet:
        return self._api_key_boundary.capture_scrub_set(
            deadline_monotonic=request.deadline_monotonic,
            cancellation=request.cancellation,
        )


class EventPluginCancellationPort:
    """Call-local cooperative cancellation bridge for CLI and local UI."""

    def __init__(self, event: Event | None = None) -> None:
        self._event = event or Event()

    def cancel(self) -> None:
        self._event.set()

    def cancellation_requested(self) -> bool:
        return self._event.is_set()


def _install_outcome(
    value: StoreInstallOutcome,
    *,
    source_path: Path,
    identity: PluginInstanceIdentity,
    diagnostics: tuple[object, ...],
    scrub_set: ProcessApiKeyScrubSet,
):
    if isinstance(value, StoreInstallResult):
        return SuccessfulPluginInstallOutcome(
            (
                PluginInstallDisposition.REPLACED
                if value.replaced
                else PluginInstallDisposition.INSTALLED
            ),
            identity,
            _safe_package_install_id(
                value.state.current_package_install_id, scrub_set
            ),
            _safe_summary(value.summary, scrub_set),
            diagnostics,
        )
    if isinstance(value, StoreAlreadyPresent):
        return AlreadyPresentPluginInstallOutcome(
            identity,
            _safe_package_install_id(
                value.state.current_package_install_id, scrub_set
            ),
            value.state.enabled,
            _safe_summary(value.summary, scrub_set),
        )
    if isinstance(value, StoreStateCutUnknown):
        return FailedPluginInstallOutcome(
            PluginInstallDisposition.ACK_UNKNOWN,
            source_path,
            identity,
            _safe_package_install_id(
                value.intended_state.current_package_install_id, scrub_set
            ),
            (),
            value.intended_state.enabled,
            value.last_known_cut,
        )
    if isinstance(value, StoreSourceInvalid):
        return FailedPluginInstallOutcome(
            PluginInstallDisposition.INVALID,
            source_path,
            identity,
            diagnostics=_safe_diagnostics(value.diagnostics, scrub_set),
        )
    if isinstance(value, StoreOperationCancelled):
        return FailedPluginInstallOutcome(
            PluginInstallDisposition.CANCELLED, source_path, identity
        )
    if isinstance(value, StoreOperationTimedOut):
        return FailedPluginInstallOutcome(
            PluginInstallDisposition.TIMED_OUT, source_path, identity
        )
    if isinstance(value, StoreMutationFailure):
        return FailedPluginInstallOutcome(
            PluginInstallDisposition.UNAVAILABLE,
            source_path,
            identity,
            _safe_optional_package_install_id(
                value.published_package_install_id, scrub_set
            ),
            _safe_diagnostics((value.diagnostic,), scrub_set),
        )
    if isinstance(value, StoreCleanupFailure):
        prior = _install_outcome(
            value.prior,
            source_path=source_path,
            identity=identity,
            diagnostics=diagnostics,
            scrub_set=scrub_set,
        )
        if isinstance(prior, CleanupUnavailablePluginInstallOutcome):
            raise TypeError("Plugin cleanup wrapper cannot recurse")
        return CleanupUnavailablePluginInstallOutcome(
            prior,
            _safe_path(value.attempted_path, scrub_set),
            value.location_status,
            _safe_diagnostics((value.diagnostic,), scrub_set)[0],
        )
    raise TypeError("Plugin store install result union is open")


def _request_identity(
    scope: PluginScopeKind, plugin_id: str, workspace_root: Path | None
) -> PluginInstanceIdentity:
    return PluginInstanceIdentity(
        scope,
        plugin_id,
        (
            workspace_scope_key(workspace_root.as_posix())
            if workspace_root is not None
            else None
        ),
    )


def _workspace_diagnostic(
    scope: PluginScopeKind, workspace_root: Path | None
) -> PluginDiagnostic | None:
    if scope is PluginScopeKind.WORKSPACE and (
        workspace_root is None or not _workspace_is_available(workspace_root)
    ):
        return _diagnostic(PluginDiagnosticCode.WORKSPACE_REQUIRED)
    return None


def _workspace_is_available(path: Path) -> bool:
    try:
        return path.is_absolute() and path.is_dir()
    except OSError:
        return False


def _enablement_failure(
    request: SetLocalPluginEnabledRequest,
    identity: PluginInstanceIdentity,
    disposition: PluginEnablementDisposition,
    code: PluginDiagnosticCode | None = None,
) -> FailedPluginEnablementOutcome:
    return FailedPluginEnablementOutcome(
        disposition,
        identity,
        request.enabled,
        diagnostics=() if code is None else (_diagnostic(code),),
    )


def _safe_summary(
    summary: PluginValidationSummary, scrub_set: ProcessApiKeyScrubSet
) -> PluginValidationSummary:
    manifest = summary.manifest
    author = manifest.author
    safe_manifest = PluginManifest(
        manifest.schema,
        _safe_text(manifest.name, scrub_set),
        _safe_optional_text(manifest.version, scrub_set),
        _safe_optional_text(manifest.description, scrub_set),
        (
            None
            if author is None
            else PluginAuthor(
                _safe_optional_text(author.name, scrub_set),
                _safe_optional_text(author.email, scrub_set),
                _safe_optional_text(author.url, scrub_set),
            )
        ),
        _safe_optional_text(manifest.homepage, scrub_set),
        _safe_optional_text(manifest.repository, scrub_set),
        _safe_optional_text(manifest.license, scrub_set),
        tuple(sorted(_safe_text(item, scrub_set) for item in manifest.keywords)),
        tuple(
            sorted(_safe_text(item, scrub_set) for item in manifest.extension_names)
        ),
    )

    skills = tuple(
        sorted(
            (
                PluginSkillSummary(
                    _safe_text(item.name, scrub_set),
                    _safe_text(item.description, scrub_set),
                    _safe_text(item.location, scrub_set),
                )
                for item in summary.skills.skills
            ),
            key=lambda item: item.name,
        )
    )
    mcp_servers = tuple(
        sorted(
            (_safe_mcp_summary(item, scrub_set) for item in summary.mcp.mcp_servers),
            key=lambda item: item.local_server_id,
        )
    )
    hooks = tuple(
        PluginHookDefinitionSummary(
            _safe_text(item.event, scrub_set),
            _safe_text(item.matcher, scrub_set),
            _safe_text(item.command, scrub_set),
            _safe_optional_text(item.command_windows, scrub_set),
            item.timeout_seconds,
            item.asynchronous,
            _safe_optional_text(item.status_message, scrub_set),
        )
        for item in summary.hooks.hook_definitions
    )
    return PluginValidationSummary(
        safe_manifest,
        PluginComponentSummary(
            summary.skills.disposition,
            skills=skills,
            diagnostics=_safe_diagnostics(summary.skills.diagnostics, scrub_set),
        ),
        PluginComponentSummary(
            summary.mcp.disposition,
            mcp_servers=mcp_servers,
            diagnostics=_safe_diagnostics(summary.mcp.diagnostics, scrub_set),
        ),
        PluginComponentSummary(
            summary.hooks.disposition,
            hook_definitions=hooks,
            diagnostics=_safe_diagnostics(summary.hooks.diagnostics, scrub_set),
        ),
    )


def _safe_enablement_outcome(value, scrub_set: ProcessApiKeyScrubSet):
    if isinstance(value, SettledPluginEnablementOutcome):
        return SettledPluginEnablementOutcome(
            value.disposition,
            _safe_identity(value.identity, scrub_set),
            _safe_package_install_id(value.package_install_id, scrub_set),
            value.enabled,
        )
    if isinstance(value, FailedPluginEnablementOutcome):
        return FailedPluginEnablementOutcome(
            disposition=value.disposition,
            identity=_safe_identity(value.identity, scrub_set),
            desired_enabled=value.desired_enabled,
            expected_package_install_id=_safe_optional_package_install_id(
                value.expected_package_install_id, scrub_set
            ),
            observed_package_install_id=_safe_optional_package_install_id(
                value.observed_package_install_id, scrub_set
            ),
            last_known_cut=_safe_optional_text(value.last_known_cut, scrub_set),
            diagnostics=_safe_diagnostics(value.diagnostics, scrub_set),
        )
    raise TypeError("Plugin enablement outcome union is open")


def _safe_removal_outcome(value, scrub_set: ProcessApiKeyScrubSet):
    if isinstance(value, RemovedPluginOutcome):
        return RemovedPluginOutcome(
            _safe_identity(value.identity, scrub_set),
            _safe_package_install_id(value.prior_package_install_id, scrub_set),
        )
    if isinstance(value, FailedPluginRemovalOutcome):
        return FailedPluginRemovalOutcome(
            disposition=value.disposition,
            identity=_safe_identity(value.identity, scrub_set),
            prior_package_install_id=_safe_optional_package_install_id(
                value.prior_package_install_id, scrub_set
            ),
            last_known_cut=_safe_optional_text(value.last_known_cut, scrub_set),
            diagnostics=_safe_diagnostics(value.diagnostics, scrub_set),
        )
    raise TypeError("Plugin removal outcome union is open")


def _safe_inspection_outcome(
    value: PluginInspectionOutcome, scrub_set: ProcessApiKeyScrubSet
) -> PluginInspectionOutcome:
    instances = tuple(
        PluginInstanceInspection(
            identity=_safe_identity(item.identity, scrub_set),
            package_install_id=_safe_package_install_id(
                item.package_install_id, scrub_set
            ),
            enabled=item.enabled,
            package_root=_safe_path(item.package_root, scrub_set),
            data_root=_safe_path(item.data_root, scrub_set),
            summary=_safe_summary(item.summary, scrub_set),
            diagnostics=_safe_diagnostics(item.diagnostics, scrub_set),
            package_in_use=item.package_in_use,
            effective_skill_names=tuple(
                sorted(
                    {
                        _safe_text(name, scrub_set)
                        for name in item.effective_skill_names
                    }
                )
            ),
            effective_mcp_server_ids=tuple(
                sorted(
                    {
                        _safe_text(server_id, scrub_set)
                        for server_id in item.effective_mcp_server_ids
                    }
                )
            ),
            effective_hook=item.effective_hook,
            effective_hook_definition_count=item.effective_hook_definition_count,
            effective_hook_trust_disposition=(
                item.effective_hook_trust_disposition
            ),
        )
        for item in value.instances
    )
    versions = tuple(
        PluginVersionInspection(
            identity=_safe_identity(item.identity, scrub_set),
            package_install_id=_safe_package_install_id(
                item.package_install_id, scrub_set
            ),
            package_root=_safe_path(item.package_root, scrub_set),
            referenced=item.referenced,
            in_use=item.in_use,
        )
        for item in value.versions
    )
    return PluginInspectionOutcome(
        disposition=value.disposition,
        instances=instances,
        versions=versions,
        diagnostics=_safe_diagnostics(value.diagnostics, scrub_set),
        physical_lifetime_anchors=value.physical_lifetime_anchors,
        skill_composition_disposition=value.skill_composition_disposition,
        mcp_composition_disposition=value.mcp_composition_disposition,
        hook_composition_disposition=value.hook_composition_disposition,
    )


def _safe_gc_outcome(
    value: PluginGcOutcome, scrub_set: ProcessApiKeyScrubSet
) -> PluginGcOutcome:
    progress = value.progress
    return PluginGcOutcome(
        value.disposition,
        PluginGcProgress(
            ordered_removed=tuple(
                _safe_gc_ref(item, scrub_set) for item in progress.ordered_removed
            ),
            ordered_in_use=tuple(
                _safe_gc_ref(item, scrub_set) for item in progress.ordered_in_use
            ),
            current_attempted_ref=(
                None
                if progress.current_attempted_ref is None
                else _safe_gc_ref(progress.current_attempted_ref, scrub_set)
            ),
            current_location_status=progress.current_location_status,
            unvisited_suffix=progress.unvisited_suffix,
        ),
        _safe_diagnostics(value.diagnostics, scrub_set),
    )


def _safe_gc_ref(
    value: PluginGcRef, scrub_set: ProcessApiKeyScrubSet
) -> PluginGcRef:
    return PluginGcRef(
        value.kind,
        _safe_path(value.path, scrub_set),
        _safe_optional_package_install_id(value.package_install_id, scrub_set),
    )


def _safe_mcp_summary(value, scrub_set: ProcessApiKeyScrubSet):
    if isinstance(value, PluginMcpStdioSummary):
        return PluginMcpStdioSummary(
            _safe_text(value.local_server_id, scrub_set),
            _safe_text(value.command, scrub_set),
            tuple(_safe_text(item, scrub_set) for item in value.args),
            _safe_optional_text(value.cwd, scrub_set),
            tuple(
                sorted(
                    (
                        _safe_text(key, scrub_set),
                        _safe_text(item, scrub_set),
                    )
                    for key, item in value.environment
                )
            ),
        )
    if isinstance(value, PluginMcpHttpSummary):
        return PluginMcpHttpSummary(
            _safe_text(value.local_server_id, scrub_set),
            _safe_text(value.endpoint, scrub_set),
            tuple(
                sorted(
                    (
                        _safe_text(key, scrub_set),
                        _safe_text(item, scrub_set),
                    )
                    for key, item in value.public_headers
                )
            ),
        )
    if isinstance(value, PluginMcpSseSummary):
        return PluginMcpSseSummary(
            _safe_text(value.local_server_id, scrub_set),
            _safe_text(value.endpoint, scrub_set),
            tuple(
                sorted(
                    (
                        _safe_text(key, scrub_set),
                        _safe_text(item, scrub_set),
                    )
                    for key, item in value.public_headers
                )
            ),
        )
    raise TypeError("Plugin MCP summary union is open")


def _safe_diagnostics(
    diagnostics: tuple[object, ...], scrub_set: ProcessApiKeyScrubSet
) -> tuple[object, ...]:
    safe: list[object] = []
    for diagnostic in diagnostics:
        if isinstance(diagnostic, PluginDiagnostic):
            safe.append(
                PluginDiagnostic(
                    diagnostic.code,
                    scrub_set.scrub_text(diagnostic.message),
                    (
                        None
                        if diagnostic.path is None
                        else scrub_set.scrub_text(diagnostic.path)
                    ),
                    _safe_optional_text(diagnostic.component, scrub_set),
                )
            )
        elif isinstance(diagnostic, SkillDiagnostic):
            safe.append(
                SkillDiagnostic(
                    diagnostic.severity,
                    diagnostic.code,
                    scrub_set.scrub_text(diagnostic.message),
                    (
                        None
                        if diagnostic.path is None
                        else _safe_path(diagnostic.path, scrub_set)
                    ),
                )
            )
        elif isinstance(diagnostic, HookDiagnostic):
            safe.append(
                HookDiagnostic(
                    scrub_set.scrub_text(diagnostic.code),
                    scrub_set.scrub_text(diagnostic.message),
                    diagnostic.severity,
                    diagnostic.event_type,
                    _safe_optional_text(diagnostic.source_label, scrub_set),
                    diagnostic.event_dispatch_ordinal,
                    diagnostic.source_ordinal,
                    diagnostic.definition_ordinal,
                    diagnostic.diagnostic_ordinal,
                )
            )
        elif isinstance(diagnostic, ProducerUnavailableCause):
            safe.append(
                ProducerUnavailableCause(
                    diagnostic.producer_kind,
                    diagnostic.reason,
                    _safe_diagnostics(diagnostic.diagnostics, scrub_set),
                )
            )
        elif isinstance(diagnostic, ResolutionUnavailableCause):
            safe_diagnostic = _safe_diagnostics(
                (diagnostic.diagnostic,), scrub_set
            )[0]
            if not isinstance(safe_diagnostic, SkillDiagnostic):
                raise TypeError("Skill resolution diagnostic union is open")
            safe.append(
                ResolutionUnavailableCause(diagnostic.reason, safe_diagnostic)
            )
        elif isinstance(diagnostic, InvalidSkillCandidateIssue):
            if _skill_issue_identity_contains_secret(diagnostic, scrub_set):
                safe.append(_withheld_skill_inspection_diagnostic())
                continue
            safe_diagnostics = _safe_diagnostics(diagnostic.diagnostics, scrub_set)
            if not all(isinstance(item, SkillDiagnostic) for item in safe_diagnostics):
                raise TypeError("invalid Skill issue diagnostic union is open")
            safe.append(
                InvalidSkillCandidateIssue(
                    diagnostic.path,
                    diagnostic.origin,
                    safe_diagnostics,
                    _safe_optional_text(diagnostic.declared_name, scrub_set),
                )
            )
        elif isinstance(
            diagnostic,
            (ShadowedSkillCandidateIssue, ConflictingSkillCandidateIssue),
        ):
            if _skill_issue_identity_contains_secret(diagnostic, scrub_set):
                safe.append(_withheld_skill_inspection_diagnostic())
            else:
                safe.append(diagnostic)
        else:
            # Other producer diagnostics carry closed enum/numeric facts only.
            safe.append(diagnostic)
    return tuple(safe)


def _skill_issue_identity_contains_secret(
    issue: object, scrub_set: ProcessApiKeyScrubSet
) -> bool:
    values: list[str] = []
    if isinstance(issue, InvalidSkillCandidateIssue):
        values.extend(
            (
                str(issue.path),
                skill_origin_label(issue.origin),
                issue.declared_name or "",
            )
        )
    elif isinstance(issue, ShadowedSkillCandidateIssue):
        values.extend(
            (
                str(issue.path),
                skill_origin_label(issue.origin),
                issue.name,
                str(issue.winner_path),
                skill_origin_label(issue.winner_origin),
            )
        )
    elif isinstance(issue, ConflictingSkillCandidateIssue):
        values.append(issue.name)
        for candidate in issue.candidates:
            values.extend(
                (str(candidate.path), skill_origin_label(candidate.origin))
            )
    else:
        raise TypeError("Skill issue union is open")
    return any(scrub_set.contains(value) for value in values)


def _withheld_skill_inspection_diagnostic() -> PluginDiagnostic:
    return PluginDiagnostic(
        PluginDiagnosticCode.VIEW_UNAVAILABLE,
        "Skill inspection fact withheld because it contained the active API key",
        component="SKILL_INSPECTION",
    )


def _safe_path(path: Path, scrub_set: ProcessApiKeyScrubSet) -> Path:
    return Path(scrub_set.scrub_text(os.fspath(path)))


def _safe_identity(
    identity: PluginInstanceIdentity, scrub_set: ProcessApiKeyScrubSet
) -> PluginInstanceIdentity:
    return PluginInstanceIdentity(
        identity.scope,
        _safe_text(identity.plugin_id, scrub_set),
        _safe_optional_text(identity.workspace_state_key, scrub_set),
    )


def _withheld_identity(scope: PluginScopeKind) -> PluginInstanceIdentity:
    return PluginInstanceIdentity(
        scope,
        "redacted-plugin",
        "redacted-workspace" if scope is PluginScopeKind.WORKSPACE else None,
    )


def _safe_package_install_id(
    value: str, scrub_set: ProcessApiKeyScrubSet
) -> str:
    if scrub_set.contains(value):
        return "pkg_00000000000000000000000000000000"
    return value


def _safe_optional_package_install_id(
    value: str | None, scrub_set: ProcessApiKeyScrubSet
) -> str | None:
    return None if value is None else _safe_package_install_id(value, scrub_set)


def _safe_optional_text(
    value: str | None, scrub_set: ProcessApiKeyScrubSet
) -> str | None:
    return None if value is None else _safe_text(value, scrub_set)


def _safe_text(value: str, scrub_set: ProcessApiKeyScrubSet) -> str:
    return scrub_set.scrub_text(value)


def _withheld_source_path() -> Path:
    return Path("[SOURCE_PATH_WITHHELD]")


def _diagnostic(code: PluginDiagnosticCode) -> PluginDiagnostic:
    return PluginDiagnostic(
        code, code.value.removeprefix("plugin_").replace("_", " ")
    )


__all__ = ["EventPluginCancellationPort", "PluginManagementService"]
