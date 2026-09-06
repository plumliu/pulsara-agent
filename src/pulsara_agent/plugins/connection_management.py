"""Plugin instance connection mutation under the existing lane and instance lock."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

from pulsara_agent.capability.mcp_management import (
    McpManagementConflict,
    McpSecretMutation,
    LocalMcpManagementService,
    _require_destination_confirmation,
    managed_bindings,
)
from pulsara_agent.plugins.contracts import (
    PluginInstanceIdentity,
    PreparedPluginInstanceObservation,
    NeverCancelPluginOperation,
)
from pulsara_agent.plugins.mcp_adapter import materialize_plugin_mcp_definition
from pulsara_agent.plugins.mcp_connection import (
    PluginMcpConnectionOverlay,
    plugin_connection_owner,
)


async def install_plugin_package(store, connections, observation, request, scrub_set):
    """One instance cut, including replacement credential settlement.

    The caller shields this complete operation. No old-instance cleanup can run
    after a later edit acquires the same lane/instance lock.
    """
    from .package_store import StoreInstallResult, StoreCleanupFailure

    identity = store.identity(
        scope=request.scope,
        plugin_id=observation.summary.manifest.name,
        workspace_root=request.workspace_root,
    )
    invalidated = []
    try:
        async with connections.lane:
            lock = store.edit_instance(
                identity,
                deadline_monotonic=request.deadline_monotonic,
                cancellation=request.cancellation,
            )
            layout, current = await asyncio.to_thread(lock.__enter__)
            try:
                if request.prepared_current is not None and (
                    identity != request.prepared_current.identity or current != request.prepared_current.current
                ):
                    raise McpManagementConflict("Plugin instance changed after model preparation")
                private = connections.settings.read()
                scope_key = plugin_connection_owner(identity, "component").scope_key
                owners = {item.binding.owner for item in private.mcp_credentials}
                owners.update(item.owner for item in private.mcp_oauth)
                owners.update(
                    plugin_connection_owner(identity, item.local_server_id)
                    for item in (current.mcp_connection_overlays if current else ())
                )
                # Include keyless OAuth components whose login is still pending.
                owners.update(
                    plugin_connection_owner(identity, server.local_server_id)
                    for server in observation.summary.mcp.mcp_servers
                )
                if current is not None and request.replace:
                    previous_summary = await asyncio.to_thread(
                        store.read_package_summary,
                        layout,
                        current.current_package_install_id,
                        deadline_monotonic=request.deadline_monotonic,
                        cancellation=request.cancellation,
                        scrub_set=scrub_set,
                    )
                    owners.update(
                        plugin_connection_owner(identity, server.local_server_id)
                        for server in previous_summary.mcp.mcp_servers
                    )
                owners = {
                    owner
                    for owner in owners
                    if owner.kind == "plugin"
                    and owner.plugin_id == identity.plugin_id
                    and owner.scope_key == scope_key
                }
                if current is None or request.replace:
                    for owner in owners:
                        invalidated.extend(connections.oauth.invalidate(owner))
                result = await asyncio.to_thread(
                    store.install_locked,
                    observation,
                    scope=request.scope,
                    workspace_root=request.workspace_root,
                    replace_existing=request.replace,
                    deadline_monotonic=request.deadline_monotonic,
                    cancellation=request.cancellation,
                    scrub_set=scrub_set,
                    current=current,
                    secret_resolver=private.mcp_secret,
                )
                published = (
                    result.prior if isinstance(result, StoreCleanupFailure) else result
                )
                if not isinstance(published, StoreInstallResult):
                    # Unconfirmed publication must not destroy values which either
                    # the old or the intended state might still reference.
                    return result
                configs = {}
                for server in observation.summary.mcp.mcp_servers:
                    try:
                        config = materialize_plugin_mcp_definition(
                            identity=identity,
                            state=published.state,
                            server=server,
                            package_root=published.package_root,
                            data_root=layout.data_root,
                            secret_resolver=private.mcp_secret,
                        )
                    except ValueError:
                        continue
                    configs[
                        plugin_connection_owner(identity, server.local_server_id)
                    ] = config
                attention = False
                for owner in owners:
                    config = configs.get(owner)
                    refs = managed_bindings(config) if config is not None else ()
                    obsolete = tuple(
                        (item.binding, None)
                        for item in private.mcp_credentials
                        if item.binding.owner == owner and item.binding not in refs
                    )
                    try:
                        if obsolete:
                            await connections.settings.replace_mcp_secrets(
                                owner, obsolete
                            )
                        grant = connections.settings.read().mcp_authorization(owner)
                        if grant is not None and (
                            config is None
                            or not connections.oauth._matches(grant, config)
                        ):
                            await connections.settings.replace_mcp_authorization(
                                owner, None, expected=grant
                            )
                    except Exception:
                        attention = True
                published = replace(published, cleanup_attention=attention)
                return (
                    replace(result, prior=published)
                    if isinstance(result, StoreCleanupFailure)
                    else published
                )
            finally:
                await asyncio.to_thread(lock.__exit__, None, None, None)
    finally:
        await asyncio.gather(*invalidated, return_exceptions=True)


@dataclass(frozen=True, slots=True)
class ReplacePluginMcpConnectionRequest:
    identity: PluginInstanceIdentity
    local_server_id: str
    expected_package_install_id: str
    expected_overlay: PluginMcpConnectionOverlay | None
    overlay: PluginMcpConnectionOverlay | None
    deadline_monotonic: float
    secret_changes: tuple[McpSecretMutation, ...] = ()
    retain_credentials_confirmed: bool = False
    prepared_current: PreparedPluginInstanceObservation | None = field(default=None, repr=False, kw_only=True)
    cancellation: NeverCancelPluginOperation = field(
        default_factory=NeverCancelPluginOperation, repr=False, compare=False
    )

    def __post_init__(self):
        if not self.local_server_id or self.deadline_monotonic <= 0:
            raise ValueError("Plugin connection request is incomplete")
        if any(
            item is not None and item.local_server_id != self.local_server_id
            for item in (self.expected_overlay, self.overlay)
        ):
            raise ValueError("Plugin connection overlay belongs to another component")


@dataclass(frozen=True, slots=True)
class PluginConnectionMutationOutcome:
    applied: bool
    cleanup_attention: bool = False


async def replace_plugin_connection(
    management,
    connections: LocalMcpManagementService,
    request: ReplacePluginMcpConnectionRequest,
):
    # The operation, including lock release and private cleanup, outlives a
    # disconnected form request. There is no durable operation/repair record.
    worker = asyncio.create_task(_replace(management, connections, request))
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    return worker.result()


async def _replace(management, connections, request):
    invalidated = []
    try:
        async with connections.lane:
            store = management._store()
            if store is None:
                raise ValueError("Plugin store is unavailable")
            cancellation = NeverCancelPluginOperation()
            lock = store.edit_instance(
                request.identity,
                deadline_monotonic=request.deadline_monotonic,
                cancellation=cancellation,
            )
            layout, current = await asyncio.to_thread(lock.__enter__)
            try:
                if (
                    current is None
                    or current.current_package_install_id
                    != request.expected_package_install_id
                ):
                    raise McpManagementConflict(
                        "Plugin package changed; refresh before editing"
                    )
                old_overlay = next(
                    (
                        item
                        for item in current.mcp_connection_overlays
                        if item.local_server_id == request.local_server_id
                    ),
                    None,
                )
                if old_overlay != request.expected_overlay:
                    raise McpManagementConflict(
                        "Plugin connection changed; refresh before editing"
                    )
                if request.prepared_current is not None and (
                    request.identity != request.prepared_current.identity or current != request.prepared_current.current
                ):
                    raise McpManagementConflict("Plugin instance changed after model preparation")
                scrub = await asyncio.to_thread(management._capture_scrub_set, request)
                summary = await asyncio.to_thread(
                    store.read_package_summary,
                    layout,
                    current.current_package_install_id,
                    deadline_monotonic=request.deadline_monotonic,
                    cancellation=cancellation,
                    scrub_set=scrub,
                )
                server = next(
                    (
                        item
                        for item in summary.mcp.mcp_servers
                        if item.local_server_id == request.local_server_id
                    ),
                    None,
                )
                if server is None:
                    raise ValueError("Plugin MCP component is not executable")
                overlays = tuple(
                    sorted(
                        (
                            item
                            for item in current.mcp_connection_overlays
                            if item.local_server_id != request.local_server_id
                        ),
                        key=lambda item: item.local_server_id,
                    )
                )
                if request.overlay is not None:
                    overlays = tuple(
                        sorted(
                            (*overlays, request.overlay),
                            key=lambda item: item.local_server_id,
                        )
                    )
                updated = replace(current, mcp_connection_overlays=overlays)
                private = connections.settings.read()

                def compose(state):
                    return materialize_plugin_mcp_definition(
                        identity=request.identity,
                        state=state,
                        server=server,
                        package_root=layout.plugin_package_parent
                        / state.current_package_install_id,
                        data_root=layout.data_root,
                        secret_resolver=private.mcp_secret,
                    )

                prior_config, next_config = compose(current), compose(updated)
                owner = plugin_connection_owner(
                    request.identity, request.local_server_id
                )
                refs = managed_bindings(next_config)
                if any(binding.owner != owner for binding in refs):
                    raise ValueError(
                        "Plugin connection references another credential owner"
                    )
                if any(change.binding not in refs for change in request.secret_changes):
                    raise ValueError(
                        "Plugin secret mutation is not referenced by this connection"
                    )
                _require_destination_confirmation(
                    prior_config,
                    next_config,
                    request.secret_changes,
                    request.retain_credentials_confirmed,
                )
                auth_changed = (
                    prior_config.auth != next_config.auth
                    or prior_config.transport != next_config.transport
                    or bool(request.secret_changes)
                )
                if auth_changed:
                    invalidated.extend(connections.oauth.invalidate(owner))
                changes = tuple(
                    (item.binding, item.value) for item in request.secret_changes
                )
                rollback = tuple(
                    (binding, private.mcp_secret(binding)) for binding, _ in changes
                )
                if changes:
                    await connections.settings.replace_mcp_secrets(owner, changes)
                try:
                    await asyncio.to_thread(
                        store.publish_connection_overlay, layout, current, overlays
                    )
                except Exception as failure:
                    try:
                        observed = await asyncio.to_thread(store.read_state, layout)
                    except (OSError, ValueError):
                        # Publication is indeterminate: do not roll back values
                        # that the now-visible definition may already reference.
                        raise ValueError(
                            "Plugin connection publication needs inspection; credentials retained"
                        ) from None
                    if observed != updated:
                        if changes:
                            await connections.settings.replace_mcp_secrets(
                                owner, rollback
                            )
                        raise failure
                attention = False
                try:
                    obsolete = tuple(
                        (item.binding, None)
                        for item in private.mcp_credentials
                        if item.binding.owner == owner and item.binding not in refs
                    )
                    if obsolete:
                        await connections.settings.replace_mcp_secrets(owner, obsolete)
                    if auth_changed:
                        grant = connections.settings.read().mcp_authorization(owner)
                        if grant is not None:
                            await connections.settings.replace_mcp_authorization(
                                owner, None, expected=grant
                            )
                except Exception:
                    attention = True
                return PluginConnectionMutationOutcome(True, attention)
            finally:
                await asyncio.to_thread(lock.__exit__, None, None, None)
    finally:
        await asyncio.gather(*invalidated, return_exceptions=True)


async def remove_plugin_state(store, connections, identity, request):
    worker = asyncio.create_task(
        _remove_plugin_state(store, connections, identity, request)
    )
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    return worker.result()


async def _remove_plugin_state(store, connections, identity, request):
    from .package_store import StoreRemovalResult

    invalidated = []
    try:
        async with connections.lane:
            lock = store.edit_instance(
                identity,
                deadline_monotonic=request.deadline_monotonic,
                cancellation=request.cancellation,
            )
            layout, current = await asyncio.to_thread(lock.__enter__)
            try:
                if current is None:
                    return StoreRemovalResult(None, False)
                if (
                    current.current_package_install_id
                    != request.expected_current_package_install_id
                    or (request.prepared_current is not None and (
                        identity != request.prepared_current.identity or current != request.prepared_current.current
                    ))
                ):
                    return StoreRemovalResult(current, False, stale=True)
                private = connections.settings.read()
                owners = {item.binding.owner for item in private.mcp_credentials}
                owners.update(item.owner for item in private.mcp_oauth)
                owners.update(
                    plugin_connection_owner(identity, item.local_server_id)
                    for item in current.mcp_connection_overlays
                )
                scope_key = plugin_connection_owner(identity, "component").scope_key
                owners = {
                    owner
                    for owner in owners
                    if owner.kind == "plugin"
                    and owner.plugin_id == identity.plugin_id
                    and owner.scope_key == scope_key
                }
                for owner in owners:
                    invalidated.extend(connections.oauth.invalidate(owner))
                result = await asyncio.to_thread(
                    store.remove_instance_locked, layout, current
                )
                if result.removed:
                    attention = False
                    for owner in owners:
                        try:
                            await connections.settings.remove_mcp_credentials(owner)
                        except Exception:
                            attention = True
                    result = replace(result, cleanup_attention=attention)
                return result
            finally:
                await asyncio.to_thread(lock.__exit__, None, None, None)
    finally:
        await asyncio.gather(*invalidated, return_exceptions=True)
