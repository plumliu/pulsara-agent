"""CLI-independent typed management boundary for local Agent Plugins 1.0."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import Event

from pulsara_agent.capability.mcp_management import McpManagementConflict

from pulsara_agent.capability.pulsara_home import (
    PulsaraHomeDisposition,
    PulsaraHomeResolution,
    resolve_pulsara_home,
)
from pulsara_agent.memory.scope import workspace_context_key
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
    PluginInspectionAbort,
    PluginInspectionAbortReason,
    PluginInspectionDisposition,
    PluginInspectionOutcome,
    PluginInstallDisposition,
    PluginInstanceIdentity,
    PreparedPluginInstanceObservation,
    PluginRemovalDisposition,
    PluginScopeKind,
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
from pulsara_agent.process_credential_boundary import (
    ProcessCredentialBoundary,
    ProcessCredentialBoundaryCancelled,
    ProcessCredentialBoundaryTimedOut,
    ProcessCredentialScrubSet,
)


class PluginManagementService:
    """The sole six-operation in-process Plugin management service."""

    def __init__(
        self,
        *,
        credential_boundary: ProcessCredentialBoundary,
        pulsara_home_resolution: PulsaraHomeResolution | None = None,
    ) -> None:
        self._credential_boundary = credential_boundary
        self._home_resolution = pulsara_home_resolution

    async def authorize_plugin_mcp(
        self,
        *,
        identity,
        local_server_id,
        expected_package_install_id,
        deadline_monotonic,
        connections,
        action="login",
    ):
        """Login uses a saved disabled or enabled instance, never a browser draft."""
        import asyncio
        from .contracts import NeverCancelPluginOperation
        from .mcp_adapter import materialize_plugin_mcp_definition
        from .mcp_connection import plugin_connection_owner

        async with connections.lane:
            store = self._store()
            if store is None:
                raise ValueError("Plugin store is unavailable")
            layout = store.layout(identity)
            state = await asyncio.to_thread(store.read_state, layout)
            if (
                state is None
                or state.current_package_install_id != expected_package_install_id
            ):
                raise McpManagementConflict("Plugin changed; refresh before logging in")
            cancellation = NeverCancelPluginOperation()
            scrub = await asyncio.to_thread(
                self._credential_boundary.capture_scrub_set,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            )
            summary = await asyncio.to_thread(
                store.read_package_summary,
                layout,
                state.current_package_install_id,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
                scrub_set=scrub,
            )
            server = next(
                (
                    server
                    for server in summary.mcp.mcp_servers
                    if server.local_server_id == local_server_id
                ),
                None,
            )
            if server is None:
                raise ValueError("Plugin MCP component is unavailable")
            config = materialize_plugin_mcp_definition(
                identity=identity,
                state=state,
                server=server,
                package_root=layout.plugin_package_parent
                / state.current_package_install_id,
                data_root=layout.data_root,
                secret_resolver=connections.settings.read().mcp_secret,
            )

            def current_target():
                return config if store.read_state(layout) == state else None

            owner = plugin_connection_owner(identity, local_server_id)
            manager = await connections.prepare_oauth()
            if action == "login":
                return manager._begin(owner, current_target)
            if action == "status":
                return manager.login_state(owner, config)

        def require_current():
            if current_target() is None:
                raise McpManagementConflict(
                    "Plugin changed; refresh before authorization changes"
                )

        if action == "logout":
            await manager.logout(owner, require_current=require_current)
        elif action == "cancel":
            await manager.cancel(owner, require_current=require_current)
        else:
            raise ValueError("invalid Plugin authorization action")
        return manager.login_state(owner, config)

    async def replace_plugin_mcp_connection_overlay(self, request, *, connections):
        from .connection_management import replace_plugin_connection

        return await replace_plugin_connection(self, connections, request)

    def validate_local_plugin_source(self, request: ValidateLocalPluginSourceRequest):
        try:
            scrub_set = self._capture_scrub_set(request)
        except ProcessCredentialBoundaryCancelled:
            return AbortedPluginValidationOutcome(
                _withheld_source_path(), PluginValidationDisposition.CANCELLED
            )
        except ProcessCredentialBoundaryTimedOut:
            return AbortedPluginValidationOutcome(
                _withheld_source_path(), PluginValidationDisposition.TIMED_OUT
            )
        observer = PluginSourceObserver(self._credential_boundary)
        try:
            from .source_import import observe_plugin_import

            observation = observe_plugin_import(observer, request, scrub_set=scrub_set)
        except PluginPackageInvalid as exc:
            return replace(InvalidPluginValidationOutcome(
                request.source_path,
                exc.diagnostics,
            ), scrub_set=scrub_set)
        except PluginPackageUnavailable as exc:
            return replace(UnavailablePluginValidationOutcome(
                request.source_path,
                (_diagnostic(exc.code),),
            ), scrub_set=scrub_set)
        except PluginPackageRaced:
            return replace(UnavailablePluginValidationOutcome(
                request.source_path,
                (_diagnostic(PluginDiagnosticCode.SOURCE_RACED),),
            ), scrub_set=scrub_set)
        except PluginPackageCancelled:
            return replace(AbortedPluginValidationOutcome(
                request.source_path,
                PluginValidationDisposition.CANCELLED,
            ), scrub_set=scrub_set)
        except PluginPackageTimedOut:
            return replace(AbortedPluginValidationOutcome(
                request.source_path,
                PluginValidationDisposition.TIMED_OUT,
            ), scrub_set=scrub_set)
        except (MemoryError, OSError, ValueError):
            return replace(UnavailablePluginValidationOutcome(
                request.source_path,
                (_diagnostic(PluginDiagnosticCode.SOURCE_UNAVAILABLE),),
            ), scrub_set=scrub_set)
        try:
            summary = observation.summary
            if observation.source_import is not None:
                source = observation.source_import
                _, _, skill_directories = source.component_plan()
                paths = {
                    str(observation.source_path / "skills" / directory.name / "SKILL.md"):
                    str(source.source / directory / "SKILL.md")
                    for directory in skill_directories
                }
                summary = replace(
                    summary,
                    skills=replace(
                        summary.skills,
                        skills=tuple(
                            replace(item, skill_path=paths[item.skill_path])
                            for item in summary.skills.skills
                        ),
                    ),
                )
            return replace(ValidPluginValidationOutcome(
                request.source_path,
                summary,
                observation.diagnostics,
            ), scrub_set=scrub_set)
        except PluginPackageCancelled:
            return replace(AbortedPluginValidationOutcome(
                request.source_path,
                PluginValidationDisposition.CANCELLED,
            ), scrub_set=scrub_set)
        except PluginPackageTimedOut:
            return replace(AbortedPluginValidationOutcome(
                request.source_path,
                PluginValidationDisposition.TIMED_OUT,
            ), scrub_set=scrub_set)
        except PluginPackageRaced:
            return replace(UnavailablePluginValidationOutcome(
                request.source_path,
                (_diagnostic(PluginDiagnosticCode.SOURCE_RACED),),
            ), scrub_set=scrub_set)
        except (KeyError, MemoryError, OSError, ValueError):
            return replace(UnavailablePluginValidationOutcome(
                request.source_path,
                (_diagnostic(PluginDiagnosticCode.SOURCE_UNAVAILABLE),),
            ), scrub_set=scrub_set)
        finally:
            observation.close()

    async def install_local_plugin(
        self, request: InstallLocalPluginRequest, *, connections
    ):
        import asyncio

        worker = asyncio.create_task(
            self._install_local_plugin(request, connections=connections)
        )
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        return worker.result()

    async def _install_local_plugin(
        self, request: InstallLocalPluginRequest, *, connections
    ):
        import asyncio
        from .connection_management import install_plugin_package

        try:
            scrub_set = await asyncio.to_thread(self._capture_scrub_set, request)
        except ProcessCredentialBoundaryCancelled:
            return FailedPluginInstallOutcome(
                PluginInstallDisposition.CANCELLED, _withheld_source_path()
            )
        except ProcessCredentialBoundaryTimedOut:
            return FailedPluginInstallOutcome(
                PluginInstallDisposition.TIMED_OUT, _withheld_source_path()
            )
        source_path = request.source_path
        store = self._store()
        if store is None:
            return FailedPluginInstallOutcome(
                PluginInstallDisposition.UNAVAILABLE,
                source_path,
                diagnostics=(
                    _diagnostic(PluginDiagnosticCode.HOME_CONFIGURATION_INVALID),
                ),
                scrub_set=scrub_set,
            )
        workspace_diagnostic = _workspace_diagnostic(
            request.scope, request.workspace_root
        )
        if workspace_diagnostic is not None:
            return replace(FailedPluginInstallOutcome(
                PluginInstallDisposition.UNAVAILABLE,
                source_path,
                diagnostics=(workspace_diagnostic,),
            ), scrub_set=scrub_set)
        observer = PluginSourceObserver(self._credential_boundary)
        try:
            from .source_import import observe_plugin_import

            observation = await asyncio.to_thread(
                observe_plugin_import, observer, request, scrub_set=scrub_set
            )
        except PluginPackageInvalid as exc:
            return replace(FailedPluginInstallOutcome(
                PluginInstallDisposition.INVALID,
                source_path,
                diagnostics=exc.diagnostics,
            ), scrub_set=scrub_set)
        except PluginPackageUnavailable as exc:
            return replace(FailedPluginInstallOutcome(
                PluginInstallDisposition.UNAVAILABLE,
                source_path,
                diagnostics=(_diagnostic(exc.code),),
            ), scrub_set=scrub_set)
        except PluginPackageRaced:
            return replace(FailedPluginInstallOutcome(
                PluginInstallDisposition.UNAVAILABLE,
                source_path,
                diagnostics=(_diagnostic(PluginDiagnosticCode.SOURCE_RACED),),
            ), scrub_set=scrub_set)
        except PluginPackageCancelled:
            return replace(FailedPluginInstallOutcome(
                PluginInstallDisposition.CANCELLED, source_path
            ), scrub_set=scrub_set)
        except PluginPackageTimedOut:
            return replace(FailedPluginInstallOutcome(
                PluginInstallDisposition.TIMED_OUT, source_path
            ), scrub_set=scrub_set)
        except (MemoryError, OSError, ValueError):
            return replace(FailedPluginInstallOutcome(
                PluginInstallDisposition.UNAVAILABLE,
                source_path,
                diagnostics=(_diagnostic(PluginDiagnosticCode.SOURCE_UNAVAILABLE),),
            ), scrub_set=scrub_set)
        try:
            identity = store.identity(
                scope=request.scope,
                plugin_id=observation.summary.manifest.name,
                workspace_root=request.workspace_root,
            )
            try:
                result = await install_plugin_package(
                    store,
                    connections,
                    observation,
                    request,
                    scrub_set,
                )
            except McpManagementConflict:
                raise
            except PluginPackageCancelled:
                result = StoreOperationCancelled()
            except PluginPackageTimedOut:
                result = StoreOperationTimedOut()
            except (MemoryError, OSError, ValueError):
                result = StoreMutationFailure(
                    _diagnostic(PluginDiagnosticCode.STATE_UNAVAILABLE)
                )
            return replace(_install_outcome(
                result,
                source_path=source_path,
                package_parent=store.layout(identity).plugin_package_parent,
                identity=identity,
                diagnostics=observation.diagnostics,
                scrub_set=scrub_set,
            ), scrub_set=scrub_set)
        finally:
            observation.close()

    def set_local_plugin_enabled(self, request: SetLocalPluginEnabledRequest):
        identity = _request_identity(
            request.scope, request.plugin_id, request.workspace_root
        )
        try:
            scrub_set = self._capture_scrub_set(request)
        except ProcessCredentialBoundaryCancelled:
            return _enablement_failure(
                request,
                _withheld_identity(request.scope),
                PluginEnablementDisposition.CANCELLED,
            )
        except ProcessCredentialBoundaryTimedOut:
            return _enablement_failure(
                request,
                _withheld_identity(request.scope),
                PluginEnablementDisposition.TIMED_OUT,
            )

        def settle(value):
            return replace(value, scrub_set=scrub_set)

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
            from pulsara_agent.settings import (
                LocalSettingsStore,
                LOCAL_SETTINGS_FILE_NAME,
            )

            result = store.set_enabled(
                identity,
                expected_package_install_id=(
                    request.expected_current_package_install_id
                ),
                enabled=request.enabled,
                connection_review=request.connection_review,
                settings=LocalSettingsStore(store.home / LOCAL_SETTINGS_FILE_NAME),
                deadline_monotonic=request.deadline_monotonic,
                cancellation=request.cancellation,
                prepared_current=request.prepared_current,
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
                    diagnostics=(_diagnostic(PluginDiagnosticCode.PACKAGE_NOT_FOUND),),
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

    def observe_local_plugin_instance(self, request, *, identity):
        """Read canonical instance identity without requiring a readable package.

        The existing mutation owner rejoins this exact value under its instance
        lock. This observation grants no permission and retains no package anchor.
        """
        from .package_core import _check_abort

        _check_abort(request.deadline_monotonic, request.cancellation)
        expected = _request_identity(identity.scope, identity.plugin_id, request.workspace_root)
        if expected != identity:
            raise ValueError("Plugin scope does not match instance observation")
        store = self._store()
        if store is None:
            raise ValueError("Plugin store is unavailable")
        layout = store.layout(identity)
        state = store.read_state(layout)
        _check_abort(request.deadline_monotonic, request.cancellation)
        return PreparedPluginInstanceObservation(identity, state)

    async def remove_local_plugin(
        self, request: RemoveLocalPluginRequest, *, connections=None
    ):
        identity = _request_identity(
            request.scope, request.plugin_id, request.workspace_root
        )
        try:
            scrub_set = self._capture_scrub_set(request)
        except ProcessCredentialBoundaryCancelled:
            return FailedPluginRemovalOutcome(
                PluginRemovalDisposition.CANCELLED,
                _withheld_identity(request.scope),
            )
        except ProcessCredentialBoundaryTimedOut:
            return FailedPluginRemovalOutcome(
                PluginRemovalDisposition.TIMED_OUT,
                _withheld_identity(request.scope),
            )

        def settle(value):
            return replace(value, scrub_set=scrub_set)

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
            from .connection_management import remove_plugin_state
            from pulsara_agent.capability.mcp_management import (
                LocalMcpManagementService,
            )
            from pulsara_agent.settings import (
                LocalSettingsStore,
                LOCAL_SETTINGS_FILE_NAME,
            )

            owns_connections = connections is None
            connections = connections or LocalMcpManagementService(
                LocalSettingsStore(store.home / LOCAL_SETTINGS_FILE_NAME),
                user_config_path=store.home / "mcp.yaml",
            )
            try:
                result = await remove_plugin_state(
                    store, connections, identity, request
                )
            finally:
                if owns_connections:
                    await connections.aclose()
        except PluginPackageCancelled:
            return settle(
                FailedPluginRemovalOutcome(PluginRemovalDisposition.CANCELLED, identity)
            )
        except PluginPackageTimedOut:
            return settle(
                FailedPluginRemovalOutcome(PluginRemovalDisposition.TIMED_OUT, identity)
            )
        if result.stale:
            return settle(
                FailedPluginRemovalOutcome(
                    PluginRemovalDisposition.STALE,
                    identity,
                    diagnostics=(_diagnostic(PluginDiagnosticCode.STATE_RACED),),
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
                    diagnostics=(_diagnostic(PluginDiagnosticCode.PACKAGE_NOT_FOUND),),
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
                    identity,
                    result.previous.current_package_install_id,
                    result.cleanup_attention,
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

    def inspect_local_plugins(
        self, request: InspectLocalPluginsRequest, *, identity=None, package_path=None
    ):
        if identity is not None and package_path is not None:
            raise ValueError("exact Plugin selectors are mutually exclusive")
        try:
            scrub_set = self._capture_scrub_set(request)
        except ProcessCredentialBoundaryCancelled:
            return PluginInspectionAbort(PluginInspectionAbortReason.CANCELLED)
        except ProcessCredentialBoundaryTimedOut:
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
            credential_boundary=self._credential_boundary,
        )
        try:
            if package_path is not None:
                try:
                    identity = store.current_package_identity_for_path(
                        package_path, workspace_root=request.workspace_root
                    )
                except (OSError, ValueError) as exc:
                    return PluginInspectionOutcome(
                        PluginInspectionDisposition.UNAVAILABLE,
                        (),
                        (),
                        (
                            PluginDiagnostic(
                                PluginDiagnosticCode.VIEW_UNAVAILABLE,
                                scrub_set.scrub_text(str(exc)),
                            ),
                        ),
                    )
                if identity is None:
                    return PluginInspectionOutcome(
                        PluginInspectionDisposition.COMPLETE, (), (), ()
                    )
            inspection = PluginInspectionService(
                store=store,
                credential_boundary=self._credential_boundary,
                pulsara_home_resolution=resolution,
            )
            arguments = dict(
                deadline_monotonic=request.deadline_monotonic,
                cancellation=request.cancellation,
                scrub_set=scrub_set,
            )
            if identity is None:
                outcome = inspection.inspect(
                    workspace_root=request.workspace_root, **arguments
                )
            else:
                expected = _request_identity(
                    identity.scope,
                    identity.plugin_id,
                    request.workspace_root
                    if identity.scope is PluginScopeKind.WORKSPACE
                    else None,
                )
                if expected != identity:
                    raise ValueError(
                        "exact Plugin identity does not join observation scope"
                    )
                outcome = inspection.inspect_exact(identity=identity, **arguments)
            return _attach_inspection_scrub(outcome, scrub_set)
        except PluginPackageCancelled:
            return PluginInspectionAbort(PluginInspectionAbortReason.CANCELLED)
        except PluginPackageTimedOut:
            return PluginInspectionAbort(PluginInspectionAbortReason.TIMED_OUT)

    def gc_local_plugin_packages(self, request: GcLocalPluginPackagesRequest):
        try:
            scrub_set = self._capture_scrub_set(request)
        except ProcessCredentialBoundaryCancelled:
            return PluginGcOutcome(
                PluginGcDisposition.CANCELLED,
                PluginGcProgress(unvisited_suffix=True),
            )
        except ProcessCredentialBoundaryTimedOut:
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
            credential_boundary=self._credential_boundary,
        )

    def _capture_scrub_set(self, request) -> ProcessCredentialScrubSet:
        return self._credential_boundary.capture_scrub_set(
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
    package_parent: Path,
    identity: PluginInstanceIdentity,
    diagnostics: tuple[object, ...],
    scrub_set: ProcessCredentialScrubSet,
):
    if isinstance(value, StoreInstallResult):
        return SuccessfulPluginInstallOutcome(
            (
                PluginInstallDisposition.REPLACED
                if value.replaced
                else PluginInstallDisposition.INSTALLED
            ),
            identity,
            value.state.current_package_install_id,
            value.summary,
            diagnostics + value.connection_diagnostics,
            package_root=package_parent / value.state.current_package_install_id,
            cleanup_attention=value.cleanup_attention,
        )
    if isinstance(value, StoreAlreadyPresent):
        return AlreadyPresentPluginInstallOutcome(
            identity,
            value.state.current_package_install_id,
            value.state.enabled,
            value.summary,
        )
    if isinstance(value, StoreStateCutUnknown):
        return FailedPluginInstallOutcome(
            PluginInstallDisposition.ACK_UNKNOWN,
            source_path,
            identity,
            value.intended_state.current_package_install_id,
            (),
            value.intended_state.enabled,
            value.last_known_cut,
        )
    if isinstance(value, StoreSourceInvalid):
        return FailedPluginInstallOutcome(
            PluginInstallDisposition.INVALID,
            source_path,
            identity,
            diagnostics=value.diagnostics,
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
            value.published_package_install_id,
            (value.diagnostic,),
        )
    if isinstance(value, StoreCleanupFailure):
        prior = _install_outcome(
            value.prior,
            source_path=source_path,
            package_parent=package_parent,
            identity=identity,
            diagnostics=diagnostics,
            scrub_set=scrub_set,
        )
        if isinstance(prior, CleanupUnavailablePluginInstallOutcome):
            raise TypeError("Plugin cleanup wrapper cannot recurse")
        return CleanupUnavailablePluginInstallOutcome(
            prior,
            value.attempted_path,
            value.location_status,
            value.diagnostic,
        )
    raise TypeError("Plugin store install result union is open")


def _request_identity(
    scope: PluginScopeKind, plugin_id: str, workspace_root: Path | None
) -> PluginInstanceIdentity:
    return PluginInstanceIdentity(
        scope,
        plugin_id,
        (
            workspace_context_key(workspace_root.as_posix())
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


def _attach_inspection_scrub(
    value: PluginInspectionOutcome, scrub_set: ProcessCredentialScrubSet
) -> PluginInspectionOutcome:
    return replace(
        value,
        instances=tuple(replace(item, scrub_set=scrub_set) for item in value.instances),
        versions=tuple(replace(item, scrub_set=scrub_set) for item in value.versions),
        scrub_set=scrub_set,
    )


def _safe_gc_outcome(
    value: PluginGcOutcome, scrub_set: ProcessCredentialScrubSet
) -> PluginGcOutcome:
    return replace(value, scrub_set=scrub_set)


def _withheld_identity(scope: PluginScopeKind) -> PluginInstanceIdentity:
    return PluginInstanceIdentity(
        scope,
        "redacted-plugin",
        "redacted-workspace" if scope is PluginScopeKind.WORKSPACE else None,
    )


def _withheld_source_path() -> Path:
    return Path("[SOURCE_PATH_WITHHELD]")


def _diagnostic(code: PluginDiagnosticCode) -> PluginDiagnostic:
    return PluginDiagnostic(code, code.value.removeprefix("plugin_").replace("_", " "))


__all__ = ["EventPluginCancellationPort", "PluginManagementService"]
