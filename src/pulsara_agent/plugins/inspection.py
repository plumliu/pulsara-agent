"""One call-local package and effective Plugin inspection owner."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from time import monotonic

from pulsara_agent.capability.bundled_skills import (
    BundledSkillDefinitionProducer,
    BundledSkillDistributionBindingOwner,
)
from pulsara_agent.capability.local_skills import (
    LooseSkillDefinitionProducer,
    SkillObservationCancelled,
)
from pulsara_agent.capability.pulsara_home import PulsaraHomeResolution
from pulsara_agent.capability.resolver import (
    EffectiveSkillCatalogDisposition,
    SkillCatalogResolver,
)
from pulsara_agent.capability.types import PluginSkillOrigin
from pulsara_agent.hooks.contracts import (
    FrozenHookDefinitionView,
    HookSourceKind,
    HookVisibilityScope,
    PluginHookSourceIdentity,
)
from pulsara_agent.hooks.trust import HookTrustStore
from pulsara_agent.mcp_config import (
    ManagedPackageMcpRuntimeSource,
    load_mcp_server_configs,
)
from pulsara_agent.plugins.contracts import (
    EnabledPluginViewDisposition,
    PluginCancellationPort,
    PluginDiagnostic,
    PluginDiagnosticCode,
    PluginInspectionComponentDisposition,
    PluginInspectionDisposition,
    PluginInspectionOutcome,
    PluginInstanceIdentity,
    PluginInstanceInspection,
    PluginMcpNormalizationDisposition,
    PluginScopeKind,
)
from pulsara_agent.plugins.hook_adapter import compose_hook_definition_view
from pulsara_agent.plugins.mcp_adapter import normalize_plugin_mcp_configs
from pulsara_agent.plugins.package_core import (
    PluginPackageCancelled,
    PluginPackageRaced,
    PluginPackageTimedOut,
    PluginPackageUnavailable,
    PluginSourceObserver,
)
from pulsara_agent.plugins.package_store import (
    ManagedPluginStore,
    PhysicalLifetimeAnchor,
)
from pulsara_agent.plugins.skill_producer import PluginSkillDefinitionProducer
from pulsara_agent.plugins.view import (
    FrozenEnabledPluginView,
    freeze_enabled_plugin_instance,
)
from pulsara_agent.process_credential_boundary import (
    ProcessCredentialBoundary,
    ProcessCredentialScrubSet,
)


class PluginInspectionService:
    """Compose list/doctor truth without retaining a cross-call cache."""

    def __init__(
        self,
        *,
        store: ManagedPluginStore,
        credential_boundary: ProcessCredentialBoundary,
        pulsara_home_resolution: PulsaraHomeResolution,
    ) -> None:
        if pulsara_home_resolution.path is None:
            raise ValueError("Plugin inspection requires a resolved Pulsara home")
        self._store = store
        self._observer = PluginSourceObserver(credential_boundary)
        self._home_resolution = pulsara_home_resolution
        self._home = pulsara_home_resolution.path

    def inspect(
        self,
        *,
        workspace_root: Path | None,
        deadline_monotonic: float,
        cancellation: PluginCancellationPort,
        scrub_set: ProcessCredentialScrubSet,
    ) -> PluginInspectionOutcome:
        current_anchors: list[PhysicalLifetimeAnchor] = []
        hook_anchors: list[object] = []
        mcp_result = None
        try:
            state_aggregate = self._store.observe_current_state_aggregate(
                workspace_root=workspace_root,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            )
            inspections: list[PluginInstanceInspection] = []
            enabled_instances = []
            for state in state_aggregate.states:
                _check_abort(deadline_monotonic, cancellation)
                identity = PluginInstanceIdentity(
                    state.scope, state.plugin_id, state.workspace_state_key
                )
                layout = self._store.layout(identity)
                package_root = (
                    layout.plugin_package_parent / state.current_package_install_id
                )
                package_in_use = self._store.package_in_use(
                    layout, state.current_package_install_id
                )
                current_anchor = self._store.acquire_package_anchor(
                    layout,
                    state.current_package_install_id,
                    deadline_monotonic=deadline_monotonic,
                    cancellation=cancellation,
                )
                current_anchors.append(current_anchor)
                hook_anchor = current_anchor.duplicate()
                hook_anchors.append(hook_anchor)
                observation = self._observer.observe(
                    package_root,
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
                    hook_lifetime_anchor=hook_anchor,
                    hook_declaration_environment=(
                        ("PLUGIN_DATA", str(layout.data_root)),
                        ("PLUGIN_ROOT", str(package_root)),
                    ),
                    scan_active_api_key=False,
                    enforce_managed_admission=False,
                )
                try:
                    if observation.summary.manifest.name != state.plugin_id:
                        raise ValueError("Plugin inspection manifest/state identity conflicts")
                    inspections.append(
                        PluginInstanceInspection(
                            identity=identity,
                            package_install_id=state.current_package_install_id,
                            enabled=state.enabled,
                            package_root=package_root,
                            data_root=layout.data_root,
                            summary=observation.summary,
                            diagnostics=observation.diagnostics,
                            package_in_use=package_in_use,
                        )
                    )
                    if state.enabled:
                        enabled_instances.append(
                            freeze_enabled_plugin_instance(
                                store=self._store,
                                identity=identity,
                                state=state,
                                package_root=package_root,
                                data_root=layout.data_root,
                                observation=observation,
                                current_anchor=current_anchor,
                                deadline_monotonic=deadline_monotonic,
                                cancellation=cancellation,
                            )
                        )
                finally:
                    observation.close()

            state_aggregate.revalidate()
            ordered_enabled = tuple(
                sorted(
                    enabled_instances,
                    key=lambda item: (
                        item.identity.scope.value,
                        item.identity.workspace_state_key or "",
                        item.identity.plugin_id,
                    ),
                )
            )
            view = FrozenEnabledPluginView(
                EnabledPluginViewDisposition.COMPLETE,
                tuple(
                    item
                    for item in ordered_enabled
                    if item.identity.scope is PluginScopeKind.USER
                ),
                tuple(
                    item
                    for item in ordered_enabled
                    if item.identity.scope is PluginScopeKind.WORKSPACE
                ),
            )

            diagnostics: list[object] = []
            skill_status = PluginInspectionComponentDisposition.COMPLETE
            winner_names_by_instance: dict[tuple[object, ...], list[str]] = {}
            try:
                _check_abort(deadline_monotonic, cancellation)
                binding = BundledSkillDistributionBindingOwner()
                try:
                    loose_producer = LooseSkillDefinitionProducer(
                        pulsara_home_resolution=self._home_resolution
                    )
                    skill_workspace = workspace_root or Path.cwd()
                    policy = loose_producer.prepare_root_policy(skill_workspace)
                    loose = loose_producer.observe(
                        policy,
                        deadline_monotonic=deadline_monotonic,
                        cancellation=cancellation,
                    )
                    plugin = PluginSkillDefinitionProducer().observe(view)
                    bundled = BundledSkillDefinitionProducer(binding).observe(
                        deadline_monotonic=deadline_monotonic,
                        cancellation=cancellation,
                    )
                    skill_inspection = SkillCatalogResolver().resolve(
                        loose, plugin, bundled
                    )
                finally:
                    binding.close()
                if (
                    skill_inspection.disposition
                    is EffectiveSkillCatalogDisposition.UNAVAILABLE
                ):
                    skill_status = PluginInspectionComponentDisposition.UNAVAILABLE
                    diagnostics.extend(skill_inspection.unavailable_causes)
                else:
                    diagnostics.extend(skill_inspection.candidate_issues)
                    for winner in skill_inspection.winners:
                        origin = winner.origin
                        if not isinstance(origin, PluginSkillOrigin):
                            continue
                        key = (
                            origin.visibility_scope.value,
                            origin.workspace_state_key,
                            origin.plugin_id,
                            origin.package_install_id,
                        )
                        winner_names_by_instance.setdefault(key, []).append(winner.name)
            except TimeoutError as exc:
                raise PluginPackageTimedOut from exc
            except SkillObservationCancelled as exc:
                raise PluginPackageCancelled from exc
            except (MemoryError, OSError, RuntimeError, ValueError) as exc:
                skill_status = PluginInspectionComponentDisposition.UNAVAILABLE
                diagnostics.append(
                    _diagnostic(
                        PluginDiagnosticCode.VIEW_UNAVAILABLE, type(exc).__name__
                    )
                )

            mcp_status = PluginInspectionComponentDisposition.COMPLETE
            mcp_ids_by_instance: dict[tuple[object, ...], list[str]] = {}
            try:
                _check_abort(deadline_monotonic, cancellation)
                existing = load_mcp_server_configs(
                    workspace_root=workspace_root,
                    trust_workspace_config=workspace_root is not None,
                )
                mcp_result = normalize_plugin_mcp_configs(
                    existing_configs=existing,
                    view=view,
                )
                diagnostics.extend(mcp_result.diagnostics)
                if (
                    mcp_result.configured_bound_exceeded
                    or mcp_result.disposition
                    is PluginMcpNormalizationDisposition.UNAVAILABLE
                ):
                    mcp_status = PluginInspectionComponentDisposition.UNAVAILABLE
                for config in mcp_result.plugin_configs:
                    source = config.runtime_source
                    if not isinstance(source, ManagedPackageMcpRuntimeSource):
                        raise TypeError("Plugin MCP normalization emitted local provenance")
                    mcp_ids_by_instance.setdefault(
                        (
                            source.store_scope_key,
                            source.package_owner_key,
                            source.package_install_id,
                        ),
                        [],
                    ).append(config.server_id)
            except (MemoryError, OSError, RuntimeError, ValueError) as exc:
                mcp_status = PluginInspectionComponentDisposition.UNAVAILABLE
                diagnostics.append(
                    _diagnostic(
                        PluginDiagnosticCode.MCP_COMPONENT_INVALID,
                        type(exc).__name__,
                    )
                )

            hook_status = PluginInspectionComponentDisposition.COMPLETE
            hooks_by_instance: dict[tuple[object, ...], tuple[int, object]] = {}
            try:
                _check_abort(deadline_monotonic, cancellation)
                hooks = compose_hook_definition_view(
                    local_view=FrozenHookDefinitionView(()),
                    plugin_view=view,
                    trust_store=HookTrustStore(self._home),
                )
                for snapshot in hooks.source_snapshots:
                    identity = snapshot.provenance.identity
                    if identity.kind is not HookSourceKind.PLUGIN:
                        raise TypeError("Plugin Hook inspection emitted local provenance")
                    if not isinstance(identity, PluginHookSourceIdentity):
                        raise TypeError("Plugin Hook identity union is open")
                    hooks_by_instance[
                        (
                            identity.visibility_scope.value,
                            identity.workspace_state_key,
                            identity.plugin_id,
                            identity.package_install_id,
                        )
                    ] = (len(snapshot.definitions), snapshot.trust.disposition)
                    diagnostics.extend(snapshot.diagnostics)
                    anchor = snapshot.provenance.physical_lifetime_anchor
                    if anchor is not None:
                        hook_anchors.append(anchor)
            except (MemoryError, OSError, RuntimeError, ValueError) as exc:
                hook_status = PluginInspectionComponentDisposition.UNAVAILABLE
                diagnostics.append(
                    _diagnostic(
                        PluginDiagnosticCode.VIEW_UNAVAILABLE, type(exc).__name__
                    )
                )

            composed: list[PluginInstanceInspection] = []
            for item in inspections:
                if not item.enabled:
                    composed.append(item)
                    continue
                scope_value = (
                    "USER" if item.identity.scope is PluginScopeKind.USER else "WORKSPACE"
                )
                skill_key = (
                    scope_value,
                    item.identity.workspace_state_key,
                    item.identity.plugin_id,
                    item.package_install_id,
                )
                mcp_scope_key = (
                    "user"
                    if item.identity.scope is PluginScopeKind.USER
                    else f"workspace:{item.identity.workspace_state_key}"
                )
                hook = hooks_by_instance.get(skill_key)
                composed.append(
                    replace(
                        item,
                        effective_skill_names=tuple(
                            sorted(winner_names_by_instance.get(skill_key, ()))
                        ),
                        effective_mcp_server_ids=tuple(
                            sorted(
                                mcp_ids_by_instance.get(
                                    (
                                        mcp_scope_key,
                                        item.identity.plugin_id,
                                        item.package_install_id,
                                    ),
                                    (),
                                )
                            )
                        ),
                        effective_hook=hook is not None,
                        effective_hook_definition_count=(0 if hook is None else hook[0]),
                        effective_hook_trust_disposition=(None if hook is None else hook[1]),
                    )
                )

            _check_abort(deadline_monotonic, cancellation)
            versions = self._store.observe_version_inventory(
                states=state_aggregate.states,
                workspace_root=workspace_root,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            )
            state_aggregate.revalidate()
            return PluginInspectionOutcome(
                disposition=PluginInspectionDisposition.COMPLETE,
                instances=tuple(
                    sorted(
                        composed,
                        key=lambda item: (
                            item.identity.scope.value,
                            item.identity.workspace_state_key or "",
                            item.identity.plugin_id,
                        ),
                    )
                ),
                versions=versions,
                diagnostics=tuple(diagnostics),
                physical_lifetime_anchors=tuple(current_anchors),
                skill_composition_disposition=skill_status,
                mcp_composition_disposition=mcp_status,
                hook_composition_disposition=hook_status,
            )
        except (PluginPackageCancelled, PluginPackageTimedOut):
            for anchor in current_anchors:
                anchor.close()
            raise
        except (
            MemoryError,
            OSError,
            RuntimeError,
            ValueError,
            PluginPackageUnavailable,
            PluginPackageRaced,
        ):
            for anchor in current_anchors:
                anchor.close()
            return PluginInspectionOutcome(
                PluginInspectionDisposition.UNAVAILABLE,
                (),
                (),
                (_diagnostic(PluginDiagnosticCode.VIEW_UNAVAILABLE),),
            )
        finally:
            if mcp_result is not None:
                mcp_result.close_plugin_anchors()
            seen: set[int] = set()
            for anchor in hook_anchors:
                if id(anchor) in seen:
                    continue
                seen.add(id(anchor))
                close = getattr(anchor, "close", None)
                if callable(close):
                    close()


def _check_abort(
    deadline_monotonic: float, cancellation: PluginCancellationPort
) -> None:
    if cancellation.cancellation_requested():
        raise PluginPackageCancelled
    if monotonic() >= deadline_monotonic:
        raise PluginPackageTimedOut


def _diagnostic(
    code: PluginDiagnosticCode, component: str | None = None
) -> PluginDiagnostic:
    return PluginDiagnostic(
        code,
        code.value.removeprefix("plugin_").replace("_", " "),
        component=component,
    )


__all__ = ["PluginInspectionService"]
