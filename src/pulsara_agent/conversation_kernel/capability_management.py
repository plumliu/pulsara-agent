"""Host-layer preparation-first routing for model capability management.

Targets are resolved from the current Host workspace and canonical source owners.
These frozen observations carry no executable adapter, secret resolver, package
anchor or permission permit. Existing mutation owners must rejoin their guards.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from pulsara_agent.capability.management_effects import (
    ResolvedCapabilityEffectProjection,
)
from pulsara_agent.capability.management_intent import (
    CapabilityManagementAction as Action,
    CapabilityManagementIntent,
    CapabilityManagementScope,
    parse_capability_management_intent,
)
from pulsara_agent.capability.mcp_management import (
    LocalMcpManagementService,
    LocalMcpTarget,
    McpManagementConflict,
    PreparedLocalMcpMutation,
    config_guard,
    config_to_entry,
    credential_presence,
    _require_destination_confirmation,
)
from pulsara_agent.mcp_config import OAuthAuthorization
from pulsara_agent.capability.local_skill_management import (
    LocalSkillManagementService,
    InstallLooseLocalSkillRequest,
    LocalSkillValidationDisposition,
    LocalSkillValidationOutcome,
    LooseSkillTargetObservation,
)
from pulsara_agent.capability.local_skill_publisher import LocalSkillInstallScope
from pulsara_agent.capability.local_skill_removal import LocalSkillRemovalIdentity
from pulsara_agent.capability.plugin_skill_contracts import FrozenPluginSkillDefinitions
from pulsara_agent.hooks.source import LocalHookSourceProvider
from pulsara_agent.hooks.contracts import (
    FrozenHookSourceSnapshot,
    FrozenHookSourceProvenance,
    HookDiagnostic,
    HookSourceSnapshotDisposition,
    HookSourceTrustAssessment,
    HookTrustDisposition,
    HookVisibilityScope,
    PluginHookSourceIdentity,
    PluginHookTrustSubject,
)
from pulsara_agent.hooks.presentation import hook_snapshot_public
from pulsara_agent.capability.inspection_presentation import diagnostic_payload
from pulsara_agent.hooks.trust import normalized_definition_digest
from pulsara_agent.plugins.contracts import (
    InspectLocalPluginsRequest,
    PluginInspectionOutcome,
    PluginInspectionDisposition,
    PluginInstanceInspection,
    PluginInstanceState,
    PluginMcpStdioSummary,
    PluginScopeKind,
    ValidateLocalPluginSourceRequest,
    ValidPluginValidationOutcome,
)
from pulsara_agent.plugins.management import PluginManagementService, _request_identity
from pulsara_agent.plugins.mcp_adapter import materialize_plugin_mcp_definition
from pulsara_agent.plugins.mcp_connection import (
    PluginConnectionReview,
    connection_editor_definition,
    connection_review,
    plugin_connection_owner,
    overlay_from_dict,
    overlay_to_dict,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
    thaw_json,
)


@dataclass(frozen=True, slots=True)
class PreparedCapabilityManagementInvocation:
    intent: CapabilityManagementIntent
    effects: ResolvedCapabilityEffectProjection
    user_inputs: tuple[str, ...]
    public_prefill: FrozenJsonObjectFact
    expected_current: FrozenJsonObjectFact
    local_mutation: PreparedLocalMcpMutation | None = None
    plugin: PluginInstanceInspection | None = None
    plugin_connection_review: tuple[PluginConnectionReview, ...] = ()
    skill_target: LooseSkillTargetObservation | None = None
    skill_validation: LocalSkillValidationOutcome | None = None
    skill_removal: LocalSkillRemovalIdentity | None = None
    hook: FrozenHookSourceSnapshot | None = None


class CapabilityManagementPreparation:
    def __init__(
        self,
        *,
        mcp: LocalMcpManagementService,
        plugins: PluginManagementService,
        workspace_root: Path,
        deadline: Callable[[], float],
        skills: LocalSkillManagementService,
        hooks: LocalHookSourceProvider,
        workspace_kind: str = "project",
        hook_review_tools: Callable | None = None,
    ):
        self.mcp = mcp
        self.plugins = plugins
        self.workspace_root = workspace_root.resolve()
        self.deadline = deadline
        self.skills = skills
        self.hooks = hooks
        self.workspace_kind = workspace_kind
        self.hook_review_tools = hook_review_tools

    async def prepare(
        self, arguments: Mapping[str, object]
    ) -> PreparedCapabilityManagementInvocation:
        intent = parse_capability_management_intent(arguments)
        fields = thaw_json(intent.fields)
        root = (
            self.workspace_root
            if intent.scope is CapabilityManagementScope.WORKSPACE
            else None
        )
        if (
            root is not None
            and self.workspace_kind != "project"
            and intent.action
            in {
                Action.INSTALL_LOOSE_SKILL,
                Action.SET_LOOSE_SKILL_ENABLED,
                Action.REMOVE_LOOSE_SKILL,
                Action.TRUST_HOOK_SOURCE,
                Action.REVOKE_HOOK_TRUST,
                Action.SET_HOOK_SOURCE_ENABLED,
                Action.INSTALL_PLUGIN,
                Action.SET_PLUGIN_ENABLED,
                Action.REMOVE_PLUGIN,
                Action.CONFIGURE_PLUGIN_MCP_CONNECTION,
            }
        ):
            raise ValueError("this operation requires a GUI project working directory")
        if intent.action in {
            Action.INSTALL_LOOSE_SKILL,
            Action.SET_LOOSE_SKILL_ENABLED,
            Action.REMOVE_LOOSE_SKILL,
        }:
            return await self._skill(intent, fields, root)
        if intent.action in {
            Action.TRUST_HOOK_SOURCE,
            Action.REVOKE_HOOK_TRUST,
            Action.SET_HOOK_SOURCE_ENABLED,
        }:
            return await self._hook(intent, fields, root)
        if intent.action in {
            Action.ADD_LOCAL_MCP,
            Action.UPDATE_LOCAL_MCP,
            Action.REMOVE_LOCAL_MCP,
        }:
            return await self._local(intent, fields, root)
        if (
            intent.action in {Action.AUTHORIZE_MCP, Action.CLEAR_MCP_AUTHORIZATION}
            and "plugin_id" not in fields
        ):
            target = LocalMcpTarget(fields["server_id"], root)
            async with self.mcp.lane:
                config = await asyncio.to_thread(self.mcp.inspect, target)
                if config is None:
                    raise McpManagementConflict("MCP connection no longer exists")
                self._exact(fields, "expected_identity", config_guard(config))
                if intent.action is Action.AUTHORIZE_MCP and not isinstance(
                    config.auth, OAuthAuthorization
                ):
                    raise ValueError("save an OAuth connection before authorizing it")
                return PreparedCapabilityManagementInvocation(
                    intent,
                    ResolvedCapabilityEffectProjection(
                        outside_workspace_write=True,
                        destructive=intent.action is Action.CLEAR_MCP_AUTHORIZATION,
                    ),
                    ("browser_authorization",)
                    if intent.action is Action.AUTHORIZE_MCP
                    else (),
                    freeze_json(
                        {**intent.to_dict(), "config": config_to_entry(config)}
                    ),
                    freeze_json({"expected_identity": config_guard(config)}),
                )
        if intent.action is Action.INSTALL_PLUGIN:
            source = Path(fields["source_path"])
            validation = await asyncio.to_thread(
                self.plugins.validate_local_plugin_source,
                ValidateLocalPluginSourceRequest(
                    source,
                    self.deadline(),
                    source_format=fields.get("source_format", "native"),
                ),
            )
            if not isinstance(validation, ValidPluginValidationOutcome):
                raise ValueError("Plugin source did not pass native package validation")
            current = await self._plugin(
                root, validation.summary.manifest.name, required=False
            )
            process = bool(
                fields.get("replace")
                and current
                and current.enabled
                and self._has_process(current)
            )
            return PreparedCapabilityManagementInvocation(
                intent,
                ResolvedCapabilityEffectProjection(
                    outside_workspace_write=True, process_control=process
                ),
                (),
                freeze_json(intent.to_dict()),
                freeze_json({"plugin_id": validation.summary.manifest.name}),
                plugin=current,
            )
        plugin = await self._plugin(root, fields["plugin_id"])
        assert plugin is not None
        self._exact(fields, "expected_package_install_id", plugin.package_install_id)
        expected = {"expected_package_install_id": plugin.package_install_id}
        user_inputs: list[str] = []
        prefill = intent.to_dict()
        review = ()
        process = False
        if intent.action is Action.SET_PLUGIN_ENABLED:
            process = fields["enabled"] != plugin.enabled and self._has_process(plugin)
            if fields["enabled"]:
                user_inputs.append("plugin_enable_review")
            review = connection_review(
                plugin.mcp_connection_overlays,
                self.mcp.settings.read().mcp_secret,
                servers=plugin.summary.mcp.mcp_servers,
                identity=plugin.identity,
            )
        elif intent.action is Action.REMOVE_PLUGIN:
            process = plugin.enabled and self._has_process(plugin)
        else:
            server = next(
                (
                    item
                    for item in plugin.summary.mcp.mcp_servers
                    if item.local_server_id == fields["server_id"]
                ),
                None,
            )
            if server is None:
                raise ValueError(
                    "Plugin MCP component does not belong to the selected instance"
                )
            old_overlay = next(
                (
                    item
                    for item in plugin.mcp_connection_overlays
                    if item.local_server_id == server.local_server_id
                ),
                None,
            )
            old = overlay_to_dict(old_overlay) if old_overlay is not None else None
            current = self._connection(plugin, server)
            if intent.action is Action.CONFIGURE_PLUGIN_MCP_CONNECTION:
                self._exact(fields, "expected_overlay", old)
                expected["expected_overlay"] = old
                if "overlay" not in fields:
                    user_inputs.append("connection_configuration")
                    candidate = current
                    prefill["overlay"] = old
                    overlay = old_overlay
                else:
                    overlay = (
                        overlay_from_dict(fields["overlay"])
                        if fields["overlay"] is not None
                        else None
                    )
                    if (
                        overlay is not None
                        and overlay.local_server_id != server.local_server_id
                    ):
                        raise ValueError(
                            "Plugin overlay references another MCP component"
                        )
                    overlays = tuple(
                        item
                        for item in plugin.mcp_connection_overlays
                        if item.local_server_id != server.local_server_id
                    )
                    if overlay is not None:
                        overlays = tuple(
                            sorted(
                                (*overlays, overlay),
                                key=lambda item: item.local_server_id,
                            )
                        )
                    candidate = self._connection(
                        replace(plugin, mcp_connection_overlays=overlays), server
                    )
                try:
                    _require_destination_confirmation(current, candidate, (), False)
                except ValueError:
                    user_inputs.append("credential_destination_confirmation")
                user_inputs.extend(
                    f"credential:{item['name']}"
                    for item in credential_presence(candidate)
                    if not item["present"]
                )
                # Keep executable/package-root fields in the immutable editor
                # definition, not a fabricated editable local-MCP cwd.
                _, prefill["config"] = connection_editor_definition(
                    server,
                    overlay,
                    owner=plugin_connection_owner(
                        plugin.identity, server.local_server_id
                    ),
                )
                process = plugin.enabled and isinstance(server, PluginMcpStdioSummary)
            elif intent.action is Action.AUTHORIZE_MCP:
                if not isinstance(current.auth, OAuthAuthorization):
                    raise ValueError(
                        "save a Plugin OAuth connection before authorizing it"
                    )
                user_inputs.append("browser_authorization")
                prefill["config"] = config_to_entry(current)
            elif intent.action is not Action.CLEAR_MCP_AUTHORIZATION:
                raise ValueError("unsupported Plugin management action")
        return PreparedCapabilityManagementInvocation(
            intent,
            ResolvedCapabilityEffectProjection(
                outside_workspace_write=True,
                process_control=process,
                destructive=intent.action
                in {Action.REMOVE_PLUGIN, Action.CLEAR_MCP_AUTHORIZATION},
            ),
            tuple(user_inputs),
            freeze_json(prefill),
            freeze_json(expected),
            plugin=plugin,
            plugin_connection_review=review,
        )

    async def _skill(self, intent, fields, root):
        scope = LocalSkillInstallScope(intent.scope.value.lower())
        effects = ResolvedCapabilityEffectProjection(
            workspace_write=root is not None, outside_workspace_write=root is None,
            destructive=intent.action is Action.REMOVE_LOOSE_SKILL,
        )
        target = validation = removal = None
        if intent.action is Action.INSTALL_LOOSE_SKILL:
            # Native publisher owns candidate overrides and exclusive publication.
            request = InstallLooseLocalSkillRequest(
                Path(fields["source_path"]),
                scope,
                root,
                fields.get("name"),
                fields.get("description"),
            )
            validation = await asyncio.to_thread(
                self.skills.validate_loose_skill_install, request
            )
            if validation.disposition is not LocalSkillValidationDisposition.VALID:
                raise ValueError("Skill source did not pass native validation")
        elif intent.action is Action.REMOVE_LOOSE_SKILL:
            removal = await asyncio.to_thread(
                self.skills.inspect_loose_skill_removal,
                skill_path=Path(fields["skill_path"]),
                scope=scope,
                workspace_root=root,
            )
        elif intent.action is Action.SET_LOOSE_SKILL_ENABLED:
            target = await asyncio.to_thread(
                self.skills.inspect_loose_skill_target,
                skill_path=Path(fields["skill_path"]),
                scope=scope,
                workspace_root=root,
            )
        return PreparedCapabilityManagementInvocation(
            intent,
            effects,
            (),
            freeze_json(intent.to_dict()),
            freeze_json({}),
            skill_target=target,
            skill_validation=validation,
            skill_removal=removal,
        )

    async def inspect_hook_sources(
        self, *, root, source_kind=None, plugin_id=None, allow_unavailable=False
    ):
        scope = "WORKSPACE" if root is not None else "USER"
        snapshots = []
        observation = {
            "plugin_inventory_status": "NOT_OBSERVED",
            "hook_composition_status": "NOT_OBSERVED",
            "diagnostics": [],
        }
        if source_kind in {None, "LOCAL"}:
            local = await asyncio.to_thread(
                self.hooks.discover,
                deadline_monotonic=self.deadline(),
                visibility_scope=HookVisibilityScope(scope),
            )
            snapshots.extend(
                (item, None)
                for item in local.source_snapshots
                if item.provenance.identity.visibility_scope.value == scope
            )
        if source_kind in {None, "PLUGIN"}:
            observed = await asyncio.to_thread(
                self.plugins.inspect_local_plugins,
                InspectLocalPluginsRequest(self.deadline(), workspace_root=root),
            )
            try:
                observation = {
                    "plugin_inventory_status": observed.disposition.value,
                    "hook_composition_status": observed.hook_composition_disposition.value,
                    "diagnostics": [
                        diagnostic_payload(item) for item in observed.diagnostics
                    ],
                }
                if (
                    observed.disposition is not PluginInspectionDisposition.COMPLETE
                    and not allow_unavailable
                ):
                    raise ValueError("Plugin Hook inventory is unavailable")
                for plugin in observed.instances:
                    if plugin.identity.scope.value != scope or (
                        plugin_id is not None and plugin.identity.plugin_id != plugin_id
                    ):
                        continue
                    if plugin.summary.hooks.disposition.value != "MISSING":
                        snapshots.append((self._plugin_hook_snapshot(plugin), plugin))
            finally:
                observed.close()
        return tuple(snapshots), observation

    def _plugin_hook_snapshot(self, plugin):
        parsed = plugin.hook_config
        if parsed is not None:
            provenance = parsed.provenance
            digest = normalized_definition_digest(provenance, parsed.definitions)
            try:
                trust = self.hooks.trust_store.assess(provenance.trust_subject, digest)
            except (OSError, ValueError):
                trust = HookSourceTrustAssessment(
                    HookTrustDisposition.UNAVAILABLE, digest, None, True, None
                )
            return FrozenHookSourceSnapshot(
                provenance,
                HookSourceSnapshotDisposition.COMPLETE,
                parsed.definitions,
                parsed.diagnostics,
                trust,
            )
        visibility = HookVisibilityScope(plugin.identity.scope.value)
        identity = PluginHookSourceIdentity(
            visibility,
            plugin.identity.plugin_id,
            plugin.package_install_id,
            "dev.pulsara/hooks/hooks.json",
            plugin.package_root / "dev.pulsara/hooks/hooks.json",
            plugin.identity.workspace_state_key,
        )
        provenance = FrozenHookSourceProvenance(
            identity,
            PluginHookTrustSubject(
                visibility,
                plugin.identity.plugin_id,
                plugin.identity.workspace_state_key,
            ),
            None,
            f"PLUGIN {visibility.value} {plugin.identity.plugin_id}",
            (
                ("PLUGIN_DATA", str(plugin.data_root)),
                ("PLUGIN_ROOT", str(plugin.package_root)),
            ),
        )
        return FrozenHookSourceSnapshot(
            provenance,
            HookSourceSnapshotDisposition.UNAVAILABLE,
            (),
            (
                HookDiagnostic(
                    "PLUGIN_HOOK_SOURCE_UNAVAILABLE",
                    "Plugin has no complete Hook definitions",
                ),
            ),
            HookSourceTrustAssessment(
                HookTrustDisposition.UNAVAILABLE, None, None, True, None
            ),
        )

    async def _hook(self, intent, fields, root):
        snapshots, _ = await self.inspect_hook_sources(
            root=root,
            source_kind=fields["source_kind"],
            plugin_id=fields.get("plugin_id"),
        )
        if len(snapshots) != 1:
            raise McpManagementConflict("Hook source is absent or ambiguous")
        snapshot, plugin = snapshots[0]
        if intent.action is Action.TRUST_HOOK_SOURCE and (
            snapshot.disposition is not HookSourceSnapshotDisposition.COMPLETE
            or snapshot.trust.disposition is HookTrustDisposition.UNAVAILABLE
        ):
            raise ValueError("complete current Hook definitions are required for trust")
        prefill = {
            **intent.to_dict(),
            "hook_source": await self.public_hook_snapshot(snapshot),
        }
        return PreparedCapabilityManagementInvocation(
            intent,
            ResolvedCapabilityEffectProjection(outside_workspace_write=True),
            ("hook_source_review",)
            if intent.action is Action.TRUST_HOOK_SOURCE
            else (),
            freeze_json(prefill),
            freeze_json({}),
            plugin=plugin,
            hook=snapshot,
        )

    def inspect_plugin_skill_definitions(self, root, *, cancellation=None, deadline_monotonic=None) -> FrozenPluginSkillDefinitions:
        # Same native package view and producer used by Host activation and CLI.
        from pulsara_agent.plugins.view import EnabledPluginViewOwner
        from pulsara_agent.plugins.skill_producer import PluginSkillDefinitionProducer
        from pulsara_agent.plugins.contracts import NeverCancelPluginOperation

        store = self.plugins._store()
        if store is None:
            raise ValueError("Plugin inventory home is unavailable")
        view = EnabledPluginViewOwner(
            store=store, credential_boundary=self.plugins._credential_boundary
        ).observe(
            workspace_root=root,
            deadline_monotonic=deadline_monotonic if deadline_monotonic is not None else self.deadline(),
            cancellation=cancellation if cancellation is not None else NeverCancelPluginOperation(),
        )
        try:
            return PluginSkillDefinitionProducer().observe(view)
        finally:
            view.close()

    async def public_hook_snapshot(self, snapshot):
        scrub = await asyncio.to_thread(
            self.plugins._capture_scrub_set, InspectLocalPluginsRequest(self.deadline())
        )
        subjects, complete = (
            self.hook_review_tools()
            if self.hook_review_tools is not None
            else ((), False)
        )
        value = hook_snapshot_public(snapshot, inspect=True)
        from pulsara_agent.hooks.presentation import add_matching_tool_review

        add_matching_tool_review(
            value,
            snapshot,
            subjects=subjects,
            complete=complete,
            workspace_root=self.workspace_root,
        )
        return scrub.scrub_json(value)

    async def _local(self, intent, fields, root):
        target = LocalMcpTarget(fields["server_id"], root)
        entry = fields.get("config")
        if intent.action is not Action.REMOVE_LOCAL_MCP and entry is None:
            async with self.mcp.lane:
                current = await asyncio.to_thread(self.mcp.inspect, target)
                if intent.action is Action.ADD_LOCAL_MCP and current is not None:
                    raise McpManagementConflict("MCP connection already exists")
                if intent.action is Action.UPDATE_LOCAL_MCP and current is None:
                    raise McpManagementConflict("MCP connection no longer exists")
                expected = config_guard(current) if current is not None else None
                self._exact(fields, "expected_identity", expected)
                prefill = {
                    **intent.to_dict(),
                    "config": config_to_entry(current) if current else {},
                }
                return PreparedCapabilityManagementInvocation(
                    intent,
                    ResolvedCapabilityEffectProjection(
                        workspace_write=root is not None,
                        outside_workspace_write=root is None,
                    ),
                    ("connection_configuration",),
                    freeze_json(prefill),
                    freeze_json({"expected_identity": expected})
                    if expected is not None
                    else freeze_json({}),
                )
        mutation = await self.mcp.prepare_mutation(
            intent.action, target, entry, expected=fields.get("expected_identity")
        )
        requirements = [f"credential:{name}" for name in mutation.missing_credentials]
        if mutation.requires_destination_confirmation:
            requirements.append("credential_destination_confirmation")
        return PreparedCapabilityManagementInvocation(
            intent,
            mutation.effects,
            tuple(requirements),
            freeze_json(
                {
                    **intent.to_dict(),
                    **({"config": thaw_json(mutation.entry)} if mutation.entry else {}),
                }
            ),
            freeze_json({"expected_identity": mutation.expected})
            if mutation.expected is not None
            else freeze_json({}),
            local_mutation=mutation,
        )

    async def _plugin(self, root, plugin_id, *, required=True):
        scope = PluginScopeKind.WORKSPACE if root is not None else PluginScopeKind.USER
        identity = _request_identity(scope, plugin_id, root)
        observed = await asyncio.to_thread(
            self.plugins.inspect_local_plugins,
            InspectLocalPluginsRequest(self.deadline(), workspace_root=root),
            identity=identity,
        )
        if not isinstance(observed, PluginInspectionOutcome):
            raise ValueError("Plugin inventory is unavailable")
        try:
            if observed.disposition is not PluginInspectionDisposition.COMPLETE:
                raise ValueError("Plugin inventory is unavailable")
            result = next(
                (item for item in observed.instances if item.identity == identity), None
            )
            if result is None and required:
                raise McpManagementConflict(
                    "Plugin is no longer installed in this scope"
                )
            return result
        finally:
            observed.close()

    def _connection(self, plugin, server):
        state = PluginInstanceState(
            plugin.identity.plugin_id,
            plugin.identity.scope,
            plugin.package_install_id,
            plugin.enabled,
            plugin.identity.workspace_state_key,
            plugin.mcp_connection_overlays,
        )
        return materialize_plugin_mcp_definition(
            identity=plugin.identity,
            state=state,
            server=server,
            package_root=plugin.package_root,
            data_root=plugin.data_root,
            secret_resolver=self.mcp.settings.read().mcp_secret,
        )

    @staticmethod
    def _has_process(plugin):
        return bool(plugin.summary.hooks.hook_definitions) or any(
            isinstance(item, PluginMcpStdioSummary)
            for item in plugin.summary.mcp.mcp_servers
        )

    @staticmethod
    def _exact(fields, name, current):
        if name in fields and fields[name] != current:
            raise McpManagementConflict(
                "capability changed; the supplied guard is stale"
            )
