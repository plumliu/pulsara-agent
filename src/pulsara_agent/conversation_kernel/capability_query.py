"""Read-only Host adaptation of native source owners and the borrowed MCP catalog.

Every instance is one invocation. No query inventory, identity registry or cached
projection survives the call. Native inspection anchors are closed after rendering.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pulsara_agent.capability.source_query import (
    CHILD_KINDS,
    CapabilityQueryError,
    minimal_row,
    plugin_source,
    plugin_target,
    public_diagnostics,
    render_page,
)
from pulsara_agent.capability.local_skill_publisher import LocalSkillInstallScope
from pulsara_agent.capability.mcp_management import LocalMcpTarget
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.mcp_config import (
    LocalConfiguredMcpRuntimeSource,
    ManagedPackageMcpRuntimeSource,
    StdioTransportConfig,
    ExactAbsoluteMcpCwd,
)
from pulsara_agent.plugins.contracts import (
    InspectLocalPluginsRequest,
    PluginInspectionAbort,
    PluginInspectionOutcome,
    PluginInspectionDisposition,
    PluginScopeKind,
)
from pulsara_agent.plugins.management import (
    EventPluginCancellationPort,
    _request_identity,
)
from pulsara_agent.plugins.mcp_adapter import (
    framed_plugin_mcp_server_id,
    plugin_configured_connection,
)
from .mcp.directory import runtime_rows, frozen_runtime_config


def transport_summary(transport):
    value = {"type": transport.kind.value}
    if isinstance(transport, StdioTransportConfig):
        value.update(
            command=transport.command,
            args=list(transport.args),
            cwd=str(transport.cwd.absolute_path)
            if isinstance(transport.cwd, ExactAbsoluteMcpCwd)
            else transport.cwd.relative_path,
        )
    else:
        value["endpoint"] = transport.endpoint
    return value


class CapabilitySourceQuery:
    def __init__(self, service, *, runtime, plan, scope):
        self.service, self.runtime, self.plan, self.scope = (
            service,
            runtime,
            plan,
            scope,
        )
        self.deadline = service.deadline() if service is not None else None
        self.cancellation = EventPluginCancellationPort()
        self.held = []
        self.diagnostics = []
        self.rows = []
        self.details = {}
        self.runtime_ids = {}
        self.unavailable_kinds = set()

    async def _native(self, function, *args, **kwargs):
        from .capability_management_execution import _settled_call

        result = await _settled_call(
            function, *args, _cancellation=self.cancellation, **kwargs
        )
        if self.cancellation.cancellation_requested():
            if isinstance(result, PluginInspectionOutcome):
                result.close()
            raise asyncio.CancelledError
        return result

    def _check(self):
        from time import monotonic

        if self.cancellation.cancellation_requested():
            raise asyncio.CancelledError
        if self.deadline is not None and monotonic() >= self.deadline:
            raise CapabilityQueryError(
                "QUERY_TIMED_OUT", "The source observation deadline expired."
            )

    def _root(self, scope):
        if scope == "USER":
            return None
        if self.service.workspace_kind != "project":
            raise CapabilityQueryError(
                "WORKSPACE_UNAVAILABLE",
                "WORKSPACE sources require the current GUI project directory.",
            )
        return self.service.workspace_root

    def _add(self, row, detail):
        self.rows.append(row)
        self.details[self._key(row["target"])] = detail

    @staticmethod
    def _key(target):
        from pulsara_agent.primitives.context import canonical_json_bytes

        return canonical_json_bytes(target)

    async def _plugins(self, target=None, *, package_path=None):
        root = (
            self._root(target["scope"])
            if target is not None
            else (
                self.service.workspace_root
                if self.service.workspace_kind == "project"
                else None
            )
        )
        identity = (
            _request_identity(
                PluginScopeKind(target["scope"]), target["plugin_id"], root
            )
            if target
            else None
        )
        result = await self._native(
            self.service.plugins.inspect_local_plugins,
            InspectLocalPluginsRequest(
                self.deadline, workspace_root=root, cancellation=self.cancellation
            ),
            identity=identity,
            package_path=package_path,
        )
        if isinstance(result, PluginInspectionAbort):
            raise CapabilityQueryError(
                f"QUERY_{result.reason.value}",
                f"Plugin source observation {result.reason.value.lower()}.",
            )
        self.held.append(result)
        if result.disposition is not PluginInspectionDisposition.COMPLETE:
            if target or package_path is not None:
                self.diagnostics.extend(public_diagnostics(result.diagnostics))
            else:
                self.diagnostics.append(
                    {
                        "code": "PLUGIN_SOURCES_INCOMPLETE",
                        "message": "Plugin capability declarations could not be fully observed; inspect an exact known source or use the GUI for package inventory.",
                    }
                )
        elif (
            target is None
            and package_path is None
            and any(
                value.value != "COMPLETE"
                for value in (
                    result.skill_composition_disposition,
                    result.mcp_composition_disposition,
                    result.hook_composition_disposition,
                )
            )
        ):
            self.diagnostics.append(
                {
                    "code": "CAPABILITY_SELECTION_UNKNOWN",
                    "message": "Some source declarations were observed, but their composition or selection could not be verified.",
                }
            )
        return result

    def _plugin_rows(self, item, *, composition=False, hook_composition=False):
        source = plugin_source(item)
        scope, plugin_id = item.identity.scope.value, item.identity.plugin_id
        disabled = "来源已禁用；" if not item.enabled else ""
        for skill in item.summary.skills.skills:
            path = item.package_root / skill.location / "SKILL.md"
            target = {"kind": "SKILL", "skill_path": str(path)}
            selected = (
                (skill.name in item.effective_skill_names) if composition else None
            )
            if not item.enabled:
                selected = False
            status = disabled + (
                "当前已选择。"
                if selected
                else "当前未选择。"
                if selected is False
                else "当前选择情况未知。"
            )
            self._add(
                minimal_row(
                    "SKILL", skill.name, target, source, status, skill.description
                ),
                {
                    "target": target,
                    "source": source,
                    "name": skill.name,
                    "description": skill.description,
                    "skill_root": str(path.parent),
                    "skill_path": str(path),
                    "selected": selected,
                    "enabled": item.enabled,
                    "manage_eligible": False,
                    "diagnostics": [],
                },
            )
        for diagnostic in item.summary.skills.diagnostics:
            path = getattr(diagnostic, "path", None)
            if path is None or Path(path).name != "SKILL.md":
                continue
            path = Path(path)
            target = {"kind": "SKILL", "skill_path": str(path)}
            if self._key(target) in self.details:
                continue
            self._add(
                minimal_row(
                    "SKILL",
                    path.parent.name,
                    target,
                    source,
                    disabled + "定义无效；不能作为 Skill 使用。",
                ),
                {
                    "target": target,
                    "source": source,
                    "skill_root": str(path.parent),
                    "skill_path": str(path),
                    "selected": False,
                    "manage_eligible": False,
                    "diagnostics": public_diagnostics(item.summary.skills.diagnostics),
                },
            )
        for server in item.summary.mcp.mcp_servers:
            target = {
                "kind": "MCP_SERVER",
                "scope": scope,
                "source_kind": "PLUGIN",
                "plugin_id": plugin_id,
                "server_id": server.local_server_id,
            }
            runtime_id = framed_plugin_mcp_server_id(plugin_id, server.local_server_id)
            config = frozen_runtime_config(self.runtime, runtime_id)
            expected_scope = (
                "user"
                if scope == "USER"
                else f"workspace:{item.identity.workspace_state_key}"
            )
            linked = (
                config is not None
                and isinstance(config.runtime_source, ManagedPackageMcpRuntimeSource)
                and config.runtime_source.store_scope_key == expected_scope
                and config.runtime_source.package_owner_key == plugin_id
                and config.runtime_source.package_install_id == item.package_install_id
            )
            detail = {
                "target": target,
                "source": source,
                "name": server.local_server_id,
                "config_path": str(item.package_root / "mcp.json"),
                "enabled": item.enabled,
                "adopted": None,
                "diagnostics": [],
            }
            try:
                detail["connection"] = transport_summary(
                    plugin_configured_connection(
                        server,
                        identity=item.identity,
                        overlays=item.mcp_connection_overlays,
                        package_root=item.package_root,
                        data_root=item.data_root,
                    )[0]
                )
            except (ValueError, OSError) as exc:
                detail["diagnostics"].append(
                    {"code": "MCP_DECLARATION_INVALID", "message": str(exc)}
                )
            status = disabled + "已声明；当前配置采用情况未知。"
            if linked:
                self.runtime_ids[self._key(target)] = runtime_id
                detail["runtime_target"] = {
                    "kind": "MCP_SERVER",
                    "runtime_server_id": runtime_id,
                }
                live = self.runtime_details.get(("MCP_SERVER", runtime_id))
                if live:
                    detail.update(
                        connection_status=live["connection_status"],
                        catalog_discovered=live["catalog_discovered"],
                    )
                    status += f"连接状态：{live['connection_status']}。"
            else:
                detail["catalog_discovered"] = False
                detail["diagnostics"].append(
                    {
                        "code": "MCP_RUNTIME_NOT_ASSOCIATED",
                        "message": "No current runtime catalog is precisely associated with this installed declaration; its remote inventory is unknown.",
                    }
                )
            self._add(
                minimal_row(
                    "MCP_SERVER", server.local_server_id, target, source, status
                ),
                detail,
            )
        if item.summary.hooks.disposition.value != "MISSING":
            target = {
                "kind": "HOOK_SOURCE",
                "scope": scope,
                "source_kind": "PLUGIN",
                "plugin_id": plugin_id,
            }
            snapshot = self.service._plugin_hook_snapshot(item)
            detail, status = self._hook_detail(snapshot, target, source)
            detail["selected"] = (
                item.effective_hook
                if hook_composition and item.enabled
                else False
                if not item.enabled
                else None
            )
            if not item.enabled:
                detail["diagnostics"].append(
                    {
                        "code": "PLUGIN_DISABLED",
                        "message": "The source Plugin is disabled; trusting these definitions does not enable the Plugin.",
                    }
                )
            self.details[self._key(target)] = detail
            if item.summary.hooks.hook_definitions:
                self._add(
                    minimal_row(
                        "HOOK_SOURCE", plugin_id, target, source, disabled + status
                    ),
                    detail,
                )
        component_diagnostics = [
            *item.summary.skills.diagnostics,
            *item.summary.mcp.diagnostics,
            *item.summary.hooks.diagnostics,
        ]
        # Exact-source requests may explain rejected declarations; a global query
        # must not turn package diagnostics into an alternate Plugin inventory.
        if self.exact_plugin:
            self.diagnostics.extend(
                public_diagnostics((*item.diagnostics, *component_diagnostics))
            )
        elif any(
            component.disposition.value in {"INVALID", "UNAVAILABLE"}
            or component.diagnostics
            for component in (item.summary.skills, item.summary.mcp, item.summary.hooks)
        ):
            generic = {
                "code": "PLUGIN_COMPONENTS_INCOMPLETE",
                "message": "Some Plugin capability declarations were rejected or could not be observed; inspect a known source for details.",
            }
            if generic not in self.diagnostics:
                self.diagnostics.append(generic)

    def _hook_detail(self, snapshot, target, source):
        trust = snapshot.trust.disposition.value
        detail = {
            "target": target,
            "source": source,
            "config_path": str(snapshot.provenance.identity.canonical_path),
            "enabled": snapshot.trust.enabled,
            "authorization": trust,
            "diagnostics": public_diagnostics(snapshot.diagnostics),
            "definitions": [
                {
                    "event": item.event_type.external_name,
                    "matcher": item.matcher.pattern,
                    "command": item.command,
                    "command_windows": item.command_windows,
                    "cwd": str(self.service.workspace_root),
                    "timeout_seconds": item.timeout_seconds,
                    "asynchronous": item.asynchronous,
                }
                for item in snapshot.definitions
            ],
        }
        from pulsara_agent.hooks.presentation import add_matching_tool_review

        subjects, complete = (
            self.service.hook_review_tools()
            if self.service.hook_review_tools
            else ((), False)
        )
        review = {"definitions": [{} for _ in snapshot.definitions]}
        add_matching_tool_review(
            review,
            snapshot,
            subjects=subjects,
            complete=complete,
            workspace_root=self.service.workspace_root,
        )
        for definition, advisory in zip(
            detail["definitions"], review["definitions"], strict=True
        ):
            if (
                complete
                and advisory["is_tool_event"]
                and not advisory["matched_operations"]
            ):
                definition["diagnostics"] = [
                    {
                        "code": "HOOK_MATCHER_NO_CURRENT_TOOL",
                        "message": "This matcher does not match a tool in the current observed tool inventory; external tool-name aliases do not translate script parameters.",
                    }
                ]
        status = (
            "已禁用；" if not snapshot.trust.enabled else ""
        ) + f"当前定义授权状态：{trust}。"
        if snapshot.disposition.value != "COMPLETE":
            status += "定义观察不完整。"
        return detail, status

    async def _local_sources(self, arguments):
        scope_filter = arguments.get("scope")
        scopes = (
            [scope_filter]
            if scope_filter
            else (
                ["USER", "WORKSPACE"]
                if self.service.workspace_kind == "project"
                else ["USER"]
            )
        )
        kinds = (
            {arguments["kind"]}
            if arguments.get("kind")
            else {"SKILL", "MCP_SERVER", "HOOK_SOURCE"}
        )
        source_filter = arguments.get("source_kind")
        plugin_definitions = None
        if "SKILL" in kinds:
            try:
                plugin_definitions = await self._native(
                    self.service.inspect_plugin_skill_definitions,
                    self.service.workspace_root
                    if self.service.workspace_kind == "project"
                    else None,
                    cancellation=self.cancellation,
                    deadline_monotonic=self.deadline,
                )
            except (ValueError, OSError, RuntimeError) as exc:
                self.diagnostics.append(
                    {"code": "SKILL_SELECTION_UNKNOWN", "message": str(exc)}
                )
        bundled = None
        winner_paths = None
        if "SKILL" in kinds:
            try:
                _, bundled, effective = await self._native(
                    self.service.skills.inspect_definition_sources,
                    workspace_root=self.service.workspace_root
                    if self.service.workspace_kind == "project"
                    else None,
                    plugin_definitions=plugin_definitions,
                    deadline_monotonic=self.deadline,
                    cancellation=self.cancellation,
                )
                if effective is not None and effective.disposition.value == "COMPLETE":
                    winner_paths = {item.path for item in effective.winners}
                else:
                    self.diagnostics.append(
                        {
                            "code": "SKILL_SELECTION_UNKNOWN",
                            "message": "Current Skill selection could not be fully observed.",
                        }
                    )
            except (OSError, ValueError) as exc:
                self.diagnostics.append(
                    {"code": "SKILL_SOURCES_INCOMPLETE", "message": str(exc)}
                )
        if "SKILL" in kinds and source_filter in {None, "LOCAL"}:
            for scope in scopes:
                self._check()
                root = self._root(scope)
                try:
                    inventory, manifests, _ = await self._native(
                        self.service.skills.inspect_loose_skills,
                        scope=LocalSkillInstallScope(scope.lower()),
                        workspace_root=root,
                        plugin_definitions=plugin_definitions,
                        deadline_monotonic=self.deadline,
                        cancellation=self.cancellation,
                    )
                except (OSError, ValueError) as exc:
                    self.diagnostics.append(
                        {"code": "SKILL_SOURCES_INCOMPLETE", "message": str(exc)}
                    )
                    continue
                if (
                    inventory["status"] != "COMPLETE"
                    or not inventory["config_available"]
                ):
                    self.diagnostics.append(
                        {
                            "code": "SKILL_SOURCES_INCOMPLETE",
                            "message": "; ".join(inventory["details"])
                            or "Skill declarations or enablement could not be fully observed.",
                        }
                    )
                by_path = {str(item.path): item for item in manifests}
                for item in inventory["items"]:
                    target = {"kind": "SKILL", "skill_path": item["path"]}
                    source = {"kind": "LOCAL", "scope": scope}
                    selected = (
                        (Path(item["path"]) in winner_paths and item["enabled"])
                        if winner_paths is not None
                        else False
                        if not item["enabled"]
                        else None
                    )
                    status = (
                        "已禁用。"
                        if not item["enabled"]
                        else "当前已选择。"
                        if selected
                        else "被其他来源覆盖。"
                        if selected is False
                        else "当前选择情况未知。"
                    )
                    manifest = by_path[item["path"]]
                    self._add(
                        minimal_row(
                            "SKILL",
                            item["name"],
                            target,
                            source,
                            status,
                            item["description"],
                        ),
                        {
                            "target": target,
                            "source": source,
                            "name": item["name"],
                            "description": item["description"],
                            "skill_root": str(manifest.base_dir),
                            "skill_path": item["path"],
                            "license": manifest.license,
                            "compatibility": manifest.compatibility,
                            "enabled": item["enabled"],
                            "selected": selected,
                            "enable_eligible": item["enable_eligible"],
                            "remove_eligible": item["remove_eligible"],
                            "diagnostics": [],
                        },
                    )
                for issue in inventory["issues"]:
                    target = {"kind": "SKILL", "skill_path": issue["path"]}
                    source = {"kind": "LOCAL", "scope": scope}
                    diagnostics = [
                        {
                            "code": "SKILL_INVALID",
                            "message": text,
                            "path": issue["path"],
                        }
                        for text in issue["details"]
                    ]
                    self._add(
                        minimal_row(
                            "SKILL",
                            Path(issue["path"]).parent.name,
                            target,
                            source,
                            "定义无效；不能作为 Skill 使用。",
                        ),
                        {
                            "target": target,
                            "source": source,
                            "skill_root": str(Path(issue["path"]).parent),
                            "skill_path": issue["path"],
                            "selected": False,
                            "enable_eligible": False,
                            "remove_eligible": issue["remove_eligible"],
                            "diagnostics": diagnostics,
                        },
                    )
                    self.diagnostics.extend(diagnostics)
        if (
            "SKILL" in kinds
            and source_filter in {None, "BUNDLED"}
            and scope_filter is None
        ):
            if bundled is None:
                candidates = ()
            else:
                candidates = bundled.candidates
            if bundled is not None and bundled.disposition.value != "COMPLETE":
                self.diagnostics.extend(
                    public_diagnostics((bundled.unavailable_cause,))
                )
            for item in candidates:
                target = {"kind": "SKILL", "skill_path": str(item.path)}
                source = {"kind": "BUNDLED"}
                selected = (
                    item.path in winner_paths if winner_paths is not None else None
                )
                self._add(
                    minimal_row(
                        "SKILL",
                        item.name,
                        target,
                        source,
                        "当前已选择。"
                        if selected
                        else "当前未选择。"
                        if selected is False
                        else "当前选择情况未知。",
                        item.description,
                    ),
                    {
                        "target": target,
                        "source": source,
                        "name": item.name,
                        "description": item.description,
                        "skill_root": str(item.base_dir),
                        "skill_path": str(item.path),
                        "selected": selected,
                        "manage_eligible": False,
                        "diagnostics": [],
                    },
                )
        if source_filter not in {None, "LOCAL"}:
            return
        if "MCP_SERVER" in kinds:
            for scope in scopes:
                self._check()
                root = self._root(scope)
                try:
                    configs = await self._native(
                        self.service.mcp.inspect_declarations, root
                    )
                except (OSError, ValueError) as exc:
                    self.unavailable_kinds.add("MCP_SERVER")
                    self.diagnostics.append(
                        {
                            "code": "MCP_CONFIG_UNAVAILABLE",
                            "message": str(exc),
                            "path": str(
                                self.service.mcp.path(LocalMcpTarget("inventory", root))
                            ),
                        }
                    )
                    continue
                for config in configs:
                    target = {
                        "kind": "MCP_SERVER",
                        "scope": scope,
                        "source_kind": "LOCAL",
                        "server_id": config.server_id,
                    }
                    source = {"kind": "LOCAL"}
                    runtime_config = frozen_runtime_config(
                        self.runtime, config.server_id
                    )
                    linked = (
                        runtime_config is not None
                        and runtime_config.runtime_source == config.runtime_source
                    )
                    status = (
                        "已禁用。"
                        if not config.enabled
                        else "已配置，尚未关联当前连接；远端目录未知。"
                    )
                    detail = {
                        "target": target,
                        "source": source,
                        "name": config.display_name,
                        "config_path": str(
                            self.service.mcp.path(
                                LocalMcpTarget(config.server_id, root)
                            )
                        ),
                        "enabled": config.enabled,
                        "connection": transport_summary(config.transport),
                        "adopted": None,
                        "diagnostics": [],
                    }
                    if linked:
                        self.runtime_ids[self._key(target)] = config.server_id
                        detail["runtime_target"] = {
                            "kind": "MCP_SERVER",
                            "runtime_server_id": config.server_id,
                        }
                        live = self.runtime_details.get(
                            ("MCP_SERVER", config.server_id)
                        )
                        if live:
                            detail.update(
                                connection_status=live["connection_status"],
                                catalog_discovered=live["catalog_discovered"],
                            )
                            status = (
                                ("安装配置已禁用；" if not config.enabled else "")
                                + f"连接状态：{live['connection_status']}；"
                                + "已保存配置的当前采用情况未知。"
                            )
                    self._add(
                        minimal_row(
                            "MCP_SERVER", config.display_name, target, source, status
                        ),
                        detail,
                    )
        if "HOOK_SOURCE" in kinds:
            for scope in scopes:
                self._root(scope)
                from pulsara_agent.hooks.contracts import HookVisibilityScope

                try:
                    view = await self._native(
                        self.service.hooks.discover,
                        deadline_monotonic=self.deadline,
                        visibility_scope=HookVisibilityScope(scope),
                    )
                except (OSError, ValueError) as exc:
                    self.unavailable_kinds.add("HOOK_SOURCE")
                    self.diagnostics.append(
                        {"code": "HOOK_SOURCE_UNAVAILABLE", "message": str(exc)}
                    )
                    continue
                for snapshot in view.source_snapshots:
                    if snapshot.provenance.identity.visibility_scope.value != scope:
                        continue
                    target = {
                        "kind": "HOOK_SOURCE",
                        "scope": scope,
                        "source_kind": "LOCAL",
                    }
                    source = {"kind": "LOCAL"}
                    detail, status = self._hook_detail(snapshot, target, source)
                    self._add(
                        minimal_row(
                            "HOOK_SOURCE", f"{scope} Hook", target, source, status
                        ),
                        detail,
                    )
                    self.diagnostics.extend(detail["diagnostics"])

    async def _inspect_skill(self, target):
        if self.service is None:
            raise CapabilityQueryError(
                "SOURCE_OBSERVATION_UNAVAILABLE",
                "No native source observation service is bound.",
            )
        observed = await self._plugins(package_path=Path(target["skill_path"]))
        if observed.disposition is not PluginInspectionDisposition.COMPLETE:
            raise CapabilityQueryError(
                "SOURCE_OBSERVATION_UNAVAILABLE",
                "The exact Skill source could not be observed.",
                diagnostics=await self._scrub(self.diagnostics),
            )
        if observed.instances:
            self.exact_plugin = plugin_target(observed.instances[0])
            for item in observed.instances:
                self._plugin_rows(item)
        else:
            await self._local_sources({"kind": "SKILL"})
        self._check()
        detail = self.details.get(self._key(target))
        if detail is None:
            raise CapabilityQueryError(
                "SOURCE_OBSERVATION_UNAVAILABLE"
                if self.diagnostics
                else "CAPABILITY_NOT_FOUND",
                "The exact Skill target was not observed.",
                diagnostics=await self._scrub(self.diagnostics),
            )
        return await self._scrub(detail)

    async def execute(self, arguments, *, inspect=False):
        self.runtime_rows, self.runtime_details, runtime_diagnostics = runtime_rows(
            self.runtime, self.plan, self.scope
        )
        self.exact_plugin = None
        try:
            target = arguments.get("target" if inspect else "parent")
            kind = target["kind"] if inspect else arguments.get("kind")
            root = self.scope is ModelInputScopeKind.ROOT
            if (
                root
                and self.service is not None
                and arguments.get("scope") == "WORKSPACE"
            ):
                self._root("WORKSPACE")
            native_target = target and (
                target["kind"] == "PLUGIN" or target.get("source_kind") == "PLUGIN"
            )
            if native_target:
                self.exact_plugin = {
                    "kind": "PLUGIN",
                    "scope": target["scope"],
                    "plugin_id": target["plugin_id"],
                }
            if inspect and kind == "SKILL":
                return await self._inspect_skill(target)
            need_sources = root and not (
                kind in CHILD_KINDS or (target and "runtime_server_id" in target)
            )
            if (
                target
                and target["kind"] == "MCP_SERVER"
                and "runtime_server_id" not in target
            ):
                need_sources = True
            if need_sources:
                if self.service is None:
                    raise CapabilityQueryError(
                        "SOURCE_OBSERVATION_UNAVAILABLE",
                        "No native source observation service is bound.",
                    )
                if "scope" in arguments:
                    self._root(arguments["scope"])
                source_filter = (
                    target.get("source_kind")
                    if target
                    else arguments.get("source_kind")
                )
                if self.exact_plugin or source_filter in {None, "PLUGIN"}:
                    observed = await self._plugins(self.exact_plugin)
                    if inspect and target["kind"] == "PLUGIN":
                        if not observed.instances:
                            raise CapabilityQueryError(
                                "PLUGIN_NOT_FOUND"
                                if observed.disposition
                                is PluginInspectionDisposition.COMPLETE
                                else "PLUGIN_UNAVAILABLE",
                                "The exact Plugin is absent or could not be observed.",
                                diagnostics=await self._scrub(self.diagnostics),
                            )
                        return await self._scrub(
                            self._plugin_detail(observed.instances[0])
                        )
                    if (
                        inspect
                        and self.exact_plugin
                        and observed.disposition
                        is not PluginInspectionDisposition.COMPLETE
                    ):
                        raise CapabilityQueryError(
                            "SOURCE_OBSERVATION_UNAVAILABLE",
                            "The exact Plugin source could not be observed.",
                            diagnostics=await self._scrub(self.diagnostics),
                        )
                    for item in observed.instances:
                        if (
                            inspect
                            and kind == "MCP_SERVER"
                            and item.summary.mcp.disposition.value
                            in {"INVALID", "UNAVAILABLE"}
                        ):
                            self.unavailable_kinds.add("MCP_SERVER")
                        self._plugin_rows(
                            item,
                            composition=not self.exact_plugin
                            and observed.skill_composition_disposition.value
                            == "COMPLETE",
                            hook_composition=not self.exact_plugin
                            and observed.hook_composition_disposition.value
                            == "COMPLETE",
                        )
                if not self.exact_plugin:
                    local_arguments = dict(arguments)
                    if target and target["kind"] == "MCP_SERVER":
                        local_arguments.update(
                            kind="MCP_SERVER",
                            scope=target.get("scope"),
                            source_kind=target.get("source_kind"),
                        )
                    if inspect and kind != "SKILL":
                        local_arguments.update(
                            kind=kind,
                            scope=target.get("scope"),
                            source_kind=target.get("source_kind"),
                        )
                    await self._local_sources(local_arguments)
            self._merge_runtime(include_servers=not self.exact_plugin)
            self._check()
            if inspect:
                detail = self.details.get(self._key(target))
                if detail is None:
                    runtime_target = (
                        "runtime_server_id" in target or kind in CHILD_KINDS
                    )
                    if runtime_target:
                        self.diagnostics.extend(runtime_diagnostics)
                        server_id = target.get(
                            "runtime_server_id", target.get("server_id")
                        )
                        live = self.runtime_details.get(("MCP_SERVER", server_id))
                        if (
                            kind in CHILD_KINDS
                            and live is not None
                            and not live["catalog_discovered"]
                        ):
                            self.diagnostics.append(
                                {
                                    "code": "MCP_CATALOG_NOT_DISCOVERED",
                                    "message": "This visible server's remote catalog has not been observed; its contents are unknown.",
                                }
                            )
                        if runtime_diagnostics or (
                            kind in CHILD_KINDS
                            and live is not None
                            and not live["catalog_discovered"]
                        ):
                            self.unavailable_kinds.add(kind)
                    unavailable = kind in self.unavailable_kinds
                    raise CapabilityQueryError(
                        "SOURCE_OBSERVATION_UNAVAILABLE"
                        if unavailable
                        else "CAPABILITY_NOT_FOUND",
                        "The exact target source could not be observed."
                        if unavailable
                        else "The exact target is not present in this caller's current observation.",
                        diagnostics=await self._scrub(self.diagnostics)
                        if unavailable
                        else (),
                    )
                return await self._scrub(detail)
            rows = self.rows
            if target:
                if target["kind"] == "PLUGIN":
                    rows = [
                        item
                        for item in rows
                        if item["kind"] not in CHILD_KINDS
                        and (item["source"] or {}).get("target") == target
                    ]
                else:
                    runtime_id = target.get(
                        "runtime_server_id"
                    ) or self.runtime_ids.get(self._key(target))
                    if runtime_id is None:
                        self.diagnostics.append(
                            {
                                "code": "MCP_RUNTIME_NOT_ASSOCIATED",
                                "message": "No runtime catalog is precisely associated; an empty page does not establish an empty remote inventory.",
                            }
                        )
                    rows = [
                        item
                        for item in rows
                        if item["kind"] in CHILD_KINDS
                        and item["target"]["server_id"] == runtime_id
                    ]
                    live = self.runtime_details.get(("MCP_SERVER", runtime_id))
                    if runtime_id and (live is None or not live["catalog_discovered"]):
                        self.diagnostics.append(
                            {
                                "code": "MCP_CATALOG_NOT_DISCOVERED",
                                "message": "The remote catalog has not been observed; its contents are unknown.",
                            }
                        )
            elif kind is None:
                rows = [item for item in rows if item["kind"] not in CHILD_KINDS]
            if kind:
                rows = [item for item in rows if item["kind"] == kind]
            if arguments.get("source_kind"):
                rows = [
                    item
                    for item in rows
                    if (item["source"] or {}).get("kind") == arguments["source_kind"]
                ]
            if arguments.get("scope"):

                def row_scope(item):
                    source = item["source"] or {}
                    return (
                        item["target"].get("scope")
                        or source.get("scope")
                        or source.get("target", {}).get("scope")
                    )

                rows = [item for item in rows if row_scope(item) == arguments["scope"]]
            if (
                kind in CHILD_KINDS
                or not root
                or (target and "runtime_server_id" in target)
            ):
                self.diagnostics.extend(runtime_diagnostics)
                if target is None:
                    for live in self.runtime_details.values():
                        if live.get("catalog_discovered") is False:
                            self.diagnostics.append(
                                {
                                    "code": "MCP_CATALOG_NOT_DISCOVERED",
                                    "message": "Some visible servers have no discovered catalog; the remote inventory is incomplete.",
                                }
                            )
                            break
            public = await self._scrub({"rows": rows, "diagnostics": self.diagnostics})
            return render_page(
                public["rows"],
                arguments,
                diagnostics=public["diagnostics"],
                completeness="PARTIAL" if self.diagnostics else "COMPLETE",
            )
        finally:
            for outcome in self.held:
                outcome.close()

    def _merge_runtime(self, *, include_servers):
        linked_sources = {}
        for row in self.rows:
            runtime_id = self.runtime_ids.get(self._key(row["target"]))
            if runtime_id:
                linked_sources[runtime_id] = dict(row["source"])
                if row["source"]["kind"] == "LOCAL" and "scope" in row["target"]:
                    linked_sources[runtime_id]["scope"] = row["target"]["scope"]
        for row in self.runtime_rows:
            target = row["target"]
            server_id = target.get("runtime_server_id") or target.get("server_id")
            if row["kind"] == "MCP_SERVER" and (
                not include_servers or server_id in linked_sources
            ):
                # Runtime targets remain inspectable even when their list row is
                # replaced by an associated installation row.
                self.details[self._key(target)] = self._runtime_detail(
                    server_id,
                    target,
                    linked_sources.get(server_id) or self._runtime_source(server_id),
                )
                continue
            projected = dict(row)
            projected["source"] = (
                (linked_sources.get(server_id) or self._runtime_source(server_id))
                if self.scope is ModelInputScopeKind.ROOT
                else None
            )
            if projected["source"] is None:
                projected["status"] += " 来源信息未在当前观察范围提供。"
            self.rows.append(projected)
            key = (
                (row["kind"], server_id)
                if row["kind"] == "MCP_SERVER"
                else (
                    row["kind"],
                    server_id,
                    target.get("uri", target.get("uri_template", target.get("name"))),
                )
            )
            detail = (
                self._runtime_detail(server_id, target, projected["source"])
                if row["kind"] == "MCP_SERVER"
                else self.runtime_details.get(key)
            )
            if detail is not None:
                self.details[self._key(target)] = {
                    **detail,
                    "source": projected["source"],
                }

    def _runtime_detail(self, server_id, target, source):
        detail = {**self.runtime_details[("MCP_SERVER", server_id)], "source": source}
        if self.scope is ModelInputScopeKind.ROOT:
            config = frozen_runtime_config(self.runtime, server_id)
            if config is not None:
                detail["connection"] = transport_summary(config.transport)
            else:
                detail["diagnostics"] = [
                    *detail["diagnostics"],
                    {
                        "code": "MCP_CONFIG_SUMMARY_UNAVAILABLE",
                        "message": "The exact runtime configuration summary could not be verified.",
                    },
                ]
        return detail

    def _runtime_source(self, server_id):
        config = frozen_runtime_config(self.runtime, server_id)
        if config is None:
            return None
        source = config.runtime_source
        if isinstance(source, LocalConfiguredMcpRuntimeSource):
            if source.source_kind.value in {"USER", "WORKSPACE"}:
                return {"kind": "LOCAL", "scope": source.source_kind.value}
            return None
        if isinstance(source, ManagedPackageMcpRuntimeSource):
            scope = "USER" if source.store_scope_key == "user" else None
            if (
                scope is None
                and self.service is not None
                and self.service.workspace_kind == "project"
            ):
                identity = _request_identity(
                    PluginScopeKind.WORKSPACE,
                    source.package_owner_key,
                    self.service.workspace_root,
                )
                if (
                    source.store_scope_key
                    == f"workspace:{identity.workspace_state_key}"
                ):
                    scope = "WORKSPACE"
            if scope is not None:
                return {
                    "kind": "PLUGIN",
                    "target": {
                        "kind": "PLUGIN",
                        "scope": scope,
                        "plugin_id": source.package_owner_key,
                    },
                }
        return None

    @staticmethod
    def _plugin_detail(item):
        def count(component, name):
            return (
                len(getattr(component, name))
                if component.disposition.value == "COMPLETE"
                else 0
                if component.disposition.value == "MISSING"
                else None
            )

        return {
            "target": plugin_target(item),
            "version": item.summary.manifest.version,
            "description": item.summary.manifest.description,
            "package_root": str(item.package_root),
            "enabled": item.enabled,
            "components": {
                "skills": {"count": count(item.summary.skills, "skills")},
                "mcp_servers": {"count": count(item.summary.mcp, "mcp_servers")},
                "hook_definitions": {
                    "count": count(item.summary.hooks, "hook_definitions")
                },
            },
            "diagnostics": public_diagnostics(
                (
                    *item.diagnostics,
                    *item.summary.skills.diagnostics,
                    *item.summary.mcp.diagnostics,
                    *item.summary.hooks.diagnostics,
                )
            ),
        }

    async def _scrub(self, value):
        if self.service is None or self.scope is not ModelInputScopeKind.ROOT:
            return value
        scrub = await self._native(
            self.service.plugins._capture_scrub_set,
            InspectLocalPluginsRequest(self.deadline, cancellation=self.cancellation),
        )
        return scrub.scrub_json(value)
