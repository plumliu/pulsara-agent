"""Complete current enabled-package observation for Host composition."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pulsara_agent.hooks.config_parser import ParsedHookConfig
from pulsara_agent.hooks.contracts import HookVisibilityScope
from pulsara_agent.plugins.contracts import (
    EnabledPluginViewDisposition,
    PluginComponentObservationDisposition,
    PluginDiagnostic,
    PluginDiagnosticCode,
    PluginInstanceIdentity,
    PluginInstanceState,
    PluginScopeKind,
    PluginValidationSummary,
)
from pulsara_agent.plugins.package_core import (
    HeldPluginPackageObservation,
    ParsedPluginMcpComponent,
    ParsedPluginSkillCandidate,
    PluginPackageCancelled,
    PluginPackageTimedOut,
    PluginSourceObserver,
)
from pulsara_agent.plugins.package_store import (
    ManagedPluginStore,
    PhysicalLifetimeAnchor,
)
from pulsara_agent.capability.types import SkillDiagnostic
from pulsara_agent.process_credential_boundary import (
    ProcessCredentialBoundary,
    ProcessCredentialScrubSet,
)


@dataclass(frozen=True, slots=True)
class FrozenPluginSkillComponentObservation:
    disposition: PluginComponentObservationDisposition
    candidates: tuple[ParsedPluginSkillCandidate, ...] = ()
    invalid_diagnostics: tuple[SkillDiagnostic, ...] = ()
    diagnostics: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        if self.disposition is PluginComponentObservationDisposition.COMPLETE:
            return
        if self.candidates:
            raise ValueError("non-complete Plugin Skill component has definitions")


@dataclass(frozen=True, slots=True)
class FrozenPluginMcpComponentObservation:
    disposition: PluginComponentObservationDisposition
    parsed: ParsedPluginMcpComponent | None = None
    diagnostics: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        if (self.disposition is PluginComponentObservationDisposition.COMPLETE) != (
            self.parsed is not None
        ):
            raise ValueError("Plugin MCP component disposition conflicts")


@dataclass(frozen=True, slots=True)
class FrozenPluginHookComponentObservation:
    disposition: PluginComponentObservationDisposition
    parsed: ParsedHookConfig | None = None
    diagnostics: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        if (self.disposition is PluginComponentObservationDisposition.COMPLETE) != (
            self.parsed is not None
        ):
            raise ValueError("Plugin Hook component disposition conflicts")


@dataclass(frozen=True, slots=True)
class FrozenEnabledPluginInstance:
    identity: PluginInstanceIdentity
    state: PluginInstanceState
    package_root: Path
    data_root: Path
    summary: PluginValidationSummary
    skills: FrozenPluginSkillComponentObservation
    mcp: FrozenPluginMcpComponentObservation
    hooks: FrozenPluginHookComponentObservation
    diagnostics: tuple[object, ...]
    physical_lifetime_anchor: PhysicalLifetimeAnchor = field(
        repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.state.enabled or (
            self.identity.plugin_id != self.state.plugin_id
            or self.identity.scope is not self.state.scope
            or self.identity.workspace_state_key != self.state.workspace_state_key
        ):
            raise ValueError("enabled Plugin instance does not join its state")
        if self.summary.manifest.name != self.identity.plugin_id:
            raise ValueError("enabled Plugin instance does not join its manifest")


@dataclass(frozen=True, slots=True)
class FrozenEnabledPluginView:
    disposition: EnabledPluginViewDisposition
    user_instances: tuple[FrozenEnabledPluginInstance, ...] = ()
    workspace_instances: tuple[FrozenEnabledPluginInstance, ...] = ()
    diagnostics: tuple[PluginDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        instances = self.instances
        if self.disposition is EnabledPluginViewDisposition.UNAVAILABLE:
            if instances or not self.diagnostics:
                raise ValueError("unavailable Plugin view contains partial truth")
            return
        keys = tuple(_instance_key(item) for item in instances)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("enabled Plugin view is not deterministic and unique")
        if any(
            item.identity.scope is not PluginScopeKind.USER
            for item in self.user_instances
        ) or any(
            item.identity.scope is not PluginScopeKind.WORKSPACE
            for item in self.workspace_instances
        ):
            raise ValueError("enabled Plugin view scope partitions conflict")

    @property
    def instances(self) -> tuple[FrozenEnabledPluginInstance, ...]:
        return (*self.user_instances, *self.workspace_instances)

    def close(self) -> None:
        for instance in self.instances:
            instance.physical_lifetime_anchor.close()


class EnabledPluginViewOwner:
    def __init__(
        self,
        *,
        store: ManagedPluginStore,
        credential_boundary: ProcessCredentialBoundary,
    ) -> None:
        self._store = store
        self._observer = PluginSourceObserver(credential_boundary)

    def current_state(self, identity):
        return self._store.read_state(self._store.layout(identity))

    def observe(
        self,
        *,
        workspace_root: Path | None,
        deadline_monotonic: float,
        cancellation,
    ) -> FrozenEnabledPluginView:
        current_anchors: list[PhysicalLifetimeAnchor] = []
        leaf_anchors: list[PhysicalLifetimeAnchor] = []
        try:
            states = self._store.observe_current_state_aggregate(
                workspace_root=workspace_root,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            )
            instances: list[FrozenEnabledPluginInstance] = []
            for state in states.states:
                if not state.enabled:
                    continue
                identity = PluginInstanceIdentity(
                    state.scope, state.plugin_id, state.workspace_state_key
                )
                layout = self._store.layout(identity)
                package_root = (
                    layout.plugin_package_parent
                    / state.current_package_install_id
                )
                current_anchor = self._store.acquire_package_anchor(
                    layout,
                    state.current_package_install_id,
                    deadline_monotonic=deadline_monotonic,
                    cancellation=cancellation,
                )
                current_anchors.append(current_anchor)
                hook_anchor = current_anchor.duplicate()
                leaf_anchors.append(hook_anchor)
                data_root = layout.data_root
                observation = self._observer.observe(
                    package_root,
                    deadline_monotonic=deadline_monotonic,
                    cancellation=cancellation,
                    scrub_set=ProcessCredentialScrubSet(),
                    hook_package_install_id=state.current_package_install_id,
                    hook_visibility=(
                        HookVisibilityScope.USER
                        if state.scope is PluginScopeKind.USER
                        else HookVisibilityScope.WORKSPACE
                    ),
                    hook_workspace_state_key=state.workspace_state_key,
                    hook_lifetime_anchor=hook_anchor,
                    hook_declaration_environment=(
                        ("PLUGIN_DATA", str(data_root)),
                        ("PLUGIN_ROOT", str(package_root)),
                    ),
                    scan_active_api_key=False,
                    enforce_managed_admission=False,
                )
                try:
                    if observation.summary.manifest.name != state.plugin_id:
                        raise OSError("Plugin state/package identity conflict")
                    instance = freeze_enabled_plugin_instance(
                        store=self._store,
                        identity=identity,
                        state=state,
                        package_root=package_root,
                        data_root=data_root,
                        observation=observation,
                        current_anchor=current_anchor,
                        deadline_monotonic=deadline_monotonic,
                        cancellation=cancellation,
                    )
                    instances.append(instance)
                    if (
                        instance.hooks.parsed is not None
                        and instance.hooks.parsed.definitions
                    ):
                        pass
                    else:
                        hook_anchor.close()
                        leaf_anchors.remove(hook_anchor)
                finally:
                    observation.close()
            states.revalidate()
            ordered = tuple(sorted(instances, key=_instance_key))
            return FrozenEnabledPluginView(
                EnabledPluginViewDisposition.COMPLETE,
                tuple(
                    item
                    for item in ordered
                    if item.identity.scope is PluginScopeKind.USER
                ),
                tuple(
                    item
                    for item in ordered
                    if item.identity.scope is PluginScopeKind.WORKSPACE
                ),
            )
        except (PluginPackageCancelled, PluginPackageTimedOut):
            for anchor in (*current_anchors, *leaf_anchors):
                anchor.close()
            raise
        except (MemoryError, OSError, RuntimeError, ValueError):
            for anchor in (*current_anchors, *leaf_anchors):
                anchor.close()
            return FrozenEnabledPluginView(
                EnabledPluginViewDisposition.UNAVAILABLE,
                diagnostics=(_diagnostic(PluginDiagnosticCode.VIEW_UNAVAILABLE),),
            )


def freeze_enabled_plugin_instance(
    *,
    store: ManagedPluginStore,
    identity: PluginInstanceIdentity,
    state: PluginInstanceState,
    package_root: Path,
    data_root: Path,
    observation: HeldPluginPackageObservation,
    current_anchor: PhysicalLifetimeAnchor,
    deadline_monotonic: float,
    cancellation,
) -> FrozenEnabledPluginInstance:
    """Lower one already-observed package without any second filesystem scan."""

    skills = FrozenPluginSkillComponentObservation(
        observation.summary.skills.disposition,
        observation.skill_candidates,
        observation.skill_invalid_diagnostics,
        observation.summary.skills.diagnostics,
    )
    mcp = FrozenPluginMcpComponentObservation(
        observation.summary.mcp.disposition,
        observation.mcp_component,
        observation.summary.mcp.diagnostics,
    )
    hooks = FrozenPluginHookComponentObservation(
        observation.summary.hooks.disposition,
        observation.hook_config,
        observation.summary.hooks.diagnostics,
    )
    process_bearing = bool(
        (mcp.parsed is not None and mcp.parsed.valid_servers)
        or (hooks.parsed is not None and hooks.parsed.definitions)
    )
    if process_bearing:
        try:
            store.ensure_instance_data_root(
                identity,
                deadline_monotonic=deadline_monotonic,
                cancellation=cancellation,
            )
        except (MemoryError, OSError, ValueError):
            diagnostic = _diagnostic(PluginDiagnosticCode.DATA_ROOT_UNAVAILABLE)
            if mcp.parsed is not None and mcp.parsed.valid_servers:
                mcp = FrozenPluginMcpComponentObservation(
                    PluginComponentObservationDisposition.UNAVAILABLE,
                    diagnostics=(diagnostic,),
                )
            if hooks.parsed is not None and hooks.parsed.definitions:
                hooks = FrozenPluginHookComponentObservation(
                    PluginComponentObservationDisposition.UNAVAILABLE,
                    diagnostics=(diagnostic,),
                )
    return FrozenEnabledPluginInstance(
        identity,
        state,
        package_root,
        data_root,
        observation.summary,
        skills,
        mcp,
        hooks,
        observation.diagnostics,
        current_anchor,
    )


def _instance_key(
    value: FrozenEnabledPluginInstance,
) -> tuple[str, str, str]:
    return (
        value.identity.scope.value,
        value.identity.workspace_state_key or "",
        value.identity.plugin_id,
    )


def _diagnostic(code: PluginDiagnosticCode) -> PluginDiagnostic:
    return PluginDiagnostic(
        code, code.value.removeprefix("plugin_").replace("_", " ")
    )


__all__ = [
    "EnabledPluginViewOwner",
    "FrozenEnabledPluginInstance",
    "FrozenEnabledPluginView",
    "FrozenPluginHookComponentObservation",
    "FrozenPluginMcpComponentObservation",
    "FrozenPluginSkillComponentObservation",
    "freeze_enabled_plugin_instance",
]
