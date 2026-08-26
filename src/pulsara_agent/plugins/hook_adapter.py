"""Compose Plugin Hook definitions into the one generic Hook future view."""

from __future__ import annotations

from dataclasses import replace

from pulsara_agent.hooks.contracts import (
    FrozenHookDefinitionView,
    FrozenHookSourceProvenance,
    FrozenHookSourceSnapshot,
    HookDiagnostic,
    HookSourceKind,
    HookSourceSnapshotDisposition,
    HookSourceTrustAssessment,
    HookTrustDisposition,
    HookVisibilityScope,
    PluginHookSourceIdentity,
    PluginHookTrustSubject,
)
from pulsara_agent.hooks.trust import HookTrustStore, normalized_definition_digest
from pulsara_agent.plugins.contracts import (
    PluginComponentObservationDisposition,
    PluginScopeKind,
)
from pulsara_agent.plugins.view import (
    FrozenEnabledPluginInstance,
    FrozenEnabledPluginView,
)


def compose_hook_definition_view(
    *,
    local_view: FrozenHookDefinitionView,
    plugin_view: FrozenEnabledPluginView,
    trust_store: HookTrustStore,
) -> FrozenHookDefinitionView:
    if any(
        item.provenance.identity.kind is HookSourceKind.PLUGIN
        for item in local_view.source_snapshots
    ):
        raise ValueError("local Hook view already contains a Plugin slice")
    user: list[FrozenHookSourceSnapshot] = []
    workspace: list[FrozenHookSourceSnapshot] = []
    for instance in _selected_instances(plugin_view):
        snapshot = _snapshot(instance, trust_store)
        if snapshot is None:
            continue
        if instance.identity.scope is PluginScopeKind.USER:
            user.append(snapshot)
        else:
            workspace.append(snapshot)
    def key(item: FrozenHookSourceSnapshot) -> str:
        identity = item.provenance.identity
        if not isinstance(identity, PluginHookSourceIdentity):
            raise TypeError("Plugin Hook slice contains local provenance")
        return identity.plugin_id

    return FrozenHookDefinitionView(
        (
            *local_view.source_snapshots,
            *sorted(user, key=key),
            *sorted(workspace, key=key),
        )
    )


def _selected_instances(
    view: FrozenEnabledPluginView,
) -> tuple[FrozenEnabledPluginInstance, ...]:
    user = {item.identity.plugin_id: item for item in view.user_instances}
    workspace = {item.identity.plugin_id: item for item in view.workspace_instances}
    selected: list[FrozenEnabledPluginInstance] = []
    for plugin_id in sorted(set(user) | set(workspace)):
        higher = workspace.get(plugin_id)
        if higher is not None and (
            higher.hooks.disposition
            is not PluginComponentObservationDisposition.MISSING
        ):
            selected.append(higher)
        elif plugin_id in user and (
            user[plugin_id].hooks.disposition
            is not PluginComponentObservationDisposition.MISSING
        ):
            selected.append(user[plugin_id])
    return tuple(selected)


def _snapshot(
    instance: FrozenEnabledPluginInstance, trust_store: HookTrustStore
) -> FrozenHookSourceSnapshot | None:
    component = instance.hooks
    if component.disposition is PluginComponentObservationDisposition.MISSING:
        return None
    if component.parsed is not None:
        provenance = component.parsed.provenance
        definitions = component.parsed.definitions
        digest = normalized_definition_digest(provenance, definitions)
        try:
            trust = trust_store.assess(provenance.trust_subject, digest)
            diagnostics = component.parsed.diagnostics
        except (OSError, ValueError) as exc:
            trust = HookSourceTrustAssessment(
                HookTrustDisposition.UNAVAILABLE, digest, None, True, None
            )
            diagnostics = (
                *component.parsed.diagnostics,
                HookDiagnostic(
                    "HOOK_TRUST_STATE_UNAVAILABLE",
                    type(exc).__name__,
                    source_label=provenance.display_label,
                ),
            )
        return FrozenHookSourceSnapshot(
            provenance,
            HookSourceSnapshotDisposition.COMPLETE,
            definitions,
            diagnostics,
            trust,
        )

    visibility = (
        HookVisibilityScope.USER
        if instance.identity.scope is PluginScopeKind.USER
        else HookVisibilityScope.WORKSPACE
    )
    anchor = instance.physical_lifetime_anchor.duplicate()
    identity = PluginHookSourceIdentity(
        visibility,
        instance.identity.plugin_id,
        instance.state.current_package_install_id,
        "dev.pulsara/hooks/hooks.json",
        instance.package_root / "dev.pulsara/hooks/hooks.json",
        instance.identity.workspace_state_key,
        anchor,
    )
    subject = PluginHookTrustSubject(
        visibility,
        instance.identity.plugin_id,
        instance.identity.workspace_state_key,
    )
    provenance = FrozenHookSourceProvenance(
        identity,
        subject,
        None,
        f"PLUGIN {visibility.value} {instance.identity.plugin_id}",
        (
            ("PLUGIN_DATA", str(instance.data_root)),
            ("PLUGIN_ROOT", str(instance.package_root)),
        ),
        anchor,
    )
    diagnostic = HookDiagnostic(
        (
            "PLUGIN_HOOK_SOURCE_INVALID"
            if component.disposition is PluginComponentObservationDisposition.INVALID
            else "PLUGIN_HOOK_SOURCE_UNAVAILABLE"
        ),
        component.disposition.value,
        source_label=provenance.display_label,
    )
    return FrozenHookSourceSnapshot(
        provenance,
        HookSourceSnapshotDisposition.UNAVAILABLE,
        (),
        (diagnostic,),
        HookSourceTrustAssessment(
            HookTrustDisposition.UNAVAILABLE, None, None, True, None
        ),
    )


def reassess_plugin_hook_snapshot(
    snapshot: FrozenHookSourceSnapshot, trust_store: HookTrustStore
) -> FrozenHookSourceSnapshot:
    if snapshot.provenance.identity.kind is not HookSourceKind.PLUGIN:
        raise ValueError("Hook trust reassessment requires Plugin provenance")
    if snapshot.disposition is not HookSourceSnapshotDisposition.COMPLETE:
        return snapshot
    digest = normalized_definition_digest(
        snapshot.provenance, snapshot.definitions
    )
    try:
        trust = trust_store.assess(snapshot.provenance.trust_subject, digest)
    except (OSError, ValueError):
        trust = HookSourceTrustAssessment(
            HookTrustDisposition.UNAVAILABLE, digest, None, True, None
        )
    return replace(snapshot, trust=trust)


__all__ = [
    "compose_hook_definition_view",
    "reassess_plugin_hook_snapshot",
]
