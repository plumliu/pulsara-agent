"""Complete capability observations through existing source and runtime owners."""

from __future__ import annotations

import asyncio
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

from pulsara_agent.capability.local_skill_publisher import LocalSkillInstallScope
from pulsara_agent.capability.source_query import CapabilityQueryError, parse_query
from pulsara_agent.conversation_kernel.capability_targets import (plugin_hook_config_path, plugin_target_from_inspection, resolve_capability_target)
from pulsara_agent.conversation_kernel.mcp.contracts import McpServerCatalogEntry
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.plugins.contracts import (InspectLocalPluginsRequest, PluginInspectionAbort,
    PluginInspectionDisposition, PluginScopeKind)
from pulsara_agent.plugins.management import EventPluginCancellationPort, _request_identity
from pulsara_agent.plugins.mcp_adapter import framed_plugin_mcp_server_id
from pulsara_agent.mcp_config import (LocalConfiguredMcpRuntimeSource, ManagedPackageMcpRuntimeSource, McpScopePolicy)
from pulsara_agent.primitives.context import canonical_json_bytes


def _text(value):
    return {"type": "text", "value": value}


def _object(value):
    return {"type": "object", "value": value}


def _status(enabled, selected=None):
    return "已禁用。" if not enabled else "当前已选择。" if selected else "当前未选择。" if selected is False else "已启用；当前选择情况未知。"


class CapabilitySourceQuery:
    def __init__(self, service, *, runtime, scope, inspect_tool, mcp_configs,
                 current_directory=None, tool_embeddings=None):
        self.service, self.runtime, self.scope = service, runtime, scope
        self.inspect_tool = inspect_tool
        self.mcp_configs = {config.server_id: config for config in mcp_configs}
        self.current_directory = current_directory
        self.tool_embeddings = tool_embeddings
        self.deadline = service.deadline() if service is not None else None
        self.cancellation = EventPluginCancellationPort()
        self.held = []
        self.content = []
        self.failed = False
        self._scrub = None

    @property
    def root(self):
        return self.service.workspace_root if self.service is not None and self.service.workspace_kind == "project" else None

    async def _native(self, function, *args, **kwargs):
        from .capability_management_execution import _settled_call
        result = await _settled_call(function, *args, _cancellation=self.cancellation, **kwargs)
        if self.cancellation.cancellation_requested():
            close = getattr(result, "close", None)
            if close is not None:
                close()
            raise asyncio.CancelledError
        self._check()
        return result

    def _check(self):
        if self.cancellation.cancellation_requested():
            raise asyncio.CancelledError
        if self.deadline is not None and monotonic() >= self.deadline:
            raise CapabilityQueryError("QUERY_TIMED_OUT", "能力来源查询超时。")

    def _failure(self, message):
        self.failed = True
        self.content.append(_text(message))

    async def _plugins(self, target=None):
        if self.service is None:
            raise CapabilityQueryError("SOURCE_UNAVAILABLE", "当前调用者没有本地能力来源查询入口。")
        identity = None
        if target is not None:
            selector = resolve_capability_target(self.service, target)
            if selector.get("source_kind") == "LOCAL":
                return ()
            identity = _request_identity(PluginScopeKind(selector["scope"]), selector["plugin_id"],
                self.root if selector["scope"] == "WORKSPACE" else None)
        outcome = await self._native(self.service.plugins.inspect_local_plugins,
            InspectLocalPluginsRequest(self.deadline, workspace_root=self.root, cancellation=self.cancellation), identity=identity)
        if isinstance(outcome, PluginInspectionAbort):
            raise CapabilityQueryError("SOURCE_UNAVAILABLE", "插件来源查询未完成。")
        self.held.append(outcome)
        if outcome.disposition is not PluginInspectionDisposition.COMPLETE:
            self.failed = True
            for diagnostic in outcome.diagnostics:
                message = str(getattr(diagnostic, "message", diagnostic))
                path = getattr(diagnostic, "path", None)
                self.content.append(_text((str(path) + ": " if path else "") + message))
            if not outcome.diagnostics:
                self.content.append(_text("部分插件来源未能完整读取。"))
        return outcome.instances

    def _scopes(self, arguments):
        target = arguments.get("target")
        if target is not None and target["kind"] in {"MCP_CONNECTION", "HOOK_SOURCE"}:
            selector = resolve_capability_target(self.service, target)
            if selector.get("source_kind") == "LOCAL":
                return [selector["scope"]]
            return []
        if arguments.get("scope") == "WORKSPACE" and self.root is None:
            raise CapabilityQueryError("WORKSPACE_UNAVAILABLE", "当前没有可用项目工作区。")
        return [arguments["scope"]] if "scope" in arguments else (["USER", "WORKSPACE"] if self.root is not None else ["USER"])

    async def _skills(self, arguments):
        target, parent = arguments.get("target"), arguments.get("parent")
        plugins = await self._plugins(parent)
        definitions = await self._native(self.service.inspect_plugin_skill_definitions, self.root,
            cancellation=self.cancellation, deadline_monotonic=self.deadline)
        _loose, bundled, effective = await self._native(self.service.skills.inspect_definition_sources,
            workspace_root=self.root, plugin_definitions=definitions,
            deadline_monotonic=self.deadline, cancellation=self.cancellation)
        winners = {item.path for item in effective.winners} if effective is not None and effective.disposition.value == "COMPLETE" else None
        records = []
        if parent is None:
            for scope in self._scopes(arguments):
                inventory, _manifests, _ = await self._native(self.service.skills.inspect_loose_skills,
                    scope=LocalSkillInstallScope(scope.lower()), workspace_root=self.root if scope == "WORKSPACE" else None,
                    plugin_definitions=definitions, deadline_monotonic=self.deadline, cancellation=self.cancellation)
                if inventory["status"] != "COMPLETE" or not inventory["config_available"]:
                    self._failure("部分技能来源或启用设置未能完整读取。")
                for item in inventory["items"]:
                    selected = Path(item["path"]) in winners if winners is not None else None
                    records.append({"target": {"kind": "SKILL", "skill_path": item["path"]},
                        "name": item["name"], "description": item["description"],
                        "source": {"kind": "LOCAL", "scope": scope}, "status": _status(item["enabled"], selected)})
                for issue in inventory["issues"]:
                    records.append({"target": {"kind": "SKILL", "skill_path": issue["path"]},
                        "source": {"kind": "LOCAL", "scope": scope}, "status": "定义无效。"})
                    if target is None or issue["path"] == target["skill_path"]:
                        self.content.extend(_text(message) for message in issue["details"])
            if "scope" not in arguments:
                if bundled.disposition.value != "COMPLETE":
                    self._failure("内置技能来源未能完整读取。")
                for item in bundled.candidates:
                    records.append({"target": {"kind": "SKILL", "skill_path": str(item.path)},
                        "name": item.name, "description": item.description, "source": {"kind": "BUNDLED"},
                        "status": _status(True, item.path in winners if winners is not None else None)})
        for plugin in plugins:
            if arguments.get("scope") not in {None, plugin.identity.scope.value}:
                continue
            if plugin.summary.skills.disposition.value not in {"COMPLETE", "MISSING"}:
                self._failure(f"插件 {plugin.summary.manifest.name} 的技能声明未能完整读取。")
            for skill in plugin.summary.skills.skills:
                records.append({"target": {"kind": "SKILL", "skill_path": skill.skill_path},
                    "name": skill.name, "description": skill.description,
                    "source": {"kind": "PLUGIN", "target": plugin_target_from_inspection(plugin)},
                    "status": _status(plugin.enabled, Path(skill.skill_path) in winners if winners is not None else None)})
        if parent is not None and arguments.get("scope") is not None:
            selector = resolve_capability_target(self.service, parent)
            if selector["scope"] != arguments["scope"]:
                raise CapabilityQueryError("INVALID_ARGUMENTS", "scope 与所选插件的归属不一致。")
        if target is not None:
            records = [item for item in records if item["target"] == target]
            if not records:
                if self.failed:
                    return
                raise CapabilityQueryError("NOT_FOUND", "未找到该技能；其说明文件或来源当前不可读取。")
        records.sort(key=lambda item: canonical_json_bytes(item["target"]))
        if target is None and records:
            self.content.insert(0, _text("技能列表按来源列出定义，范围包括已禁用和当前未选择的副本。\n"))
        self.content.extend(_object(item) for item in records)
        if not records:
            self.content.append(_text("没有找到符合条件的技能。"))

    def _hook_record(self, snapshot):
        from pulsara_agent.hooks.presentation import hook_model_enabled
        trust = snapshot.trust.disposition.value
        trusted = {"TRUSTED": "已信任", "UNTRUSTED": "尚未信任", "STALE": "定义已变化，需重新审阅"}.get(trust, trust)
        enabled = hook_model_enabled(snapshot)
        enabled_text = "已启用；" if enabled is True else "已停用；" if enabled is False else "启用状态未知；"
        return {"target": {"kind": "HOOK_SOURCE", "config_path": str(snapshot.provenance.identity.canonical_path)},
            "status": enabled_text + trusted + "。",
            "definitions": [{"event": item.event_type.external_name, "matcher": item.matcher.pattern, "command": item.command}
                for item in snapshot.definitions]}

    async def _hooks(self, arguments):
        from pulsara_agent.hooks.contracts import HookVisibilityScope
        target, parent = arguments.get("target"), arguments.get("parent")
        records = []
        plugins = await self._plugins(parent or target)
        if parent is None:
            for scope in self._scopes(arguments):
                view = await self._native(self.service.hooks.discover,
                    deadline_monotonic=self.deadline, visibility_scope=HookVisibilityScope(scope))
                for snapshot in view.source_snapshots:
                    if snapshot.provenance.identity.visibility_scope.value != scope:
                        continue
                    if snapshot.disposition.value == "MISSING":
                        continue
                    records.append(self._hook_record(snapshot))
                    if snapshot.disposition.value != "COMPLETE":
                        self._failure("部分 Hook 来源未能完整读取。")
        for plugin in plugins:
            if arguments.get("scope") not in {None, plugin.identity.scope.value}:
                continue
            if plugin.summary.hooks.disposition.value == "MISSING":
                continue
            if plugin.summary.hooks.disposition.value != "COMPLETE":
                self._failure(f"插件 {plugin.summary.manifest.name} 的 Hook 定义未能完整读取。")
            if plugin_hook_config_path(plugin) is None:
                continue
            records.append(self._hook_record(self.service._plugin_hook_snapshot(plugin)))
        if target is not None:
            records = [item for item in records if item["target"] == target]
            if not records:
                if self.failed:
                    return
                raise CapabilityQueryError("NOT_FOUND", "未找到该 Hook 来源。")
        if parent is not None and arguments.get("scope") not in {None, resolve_capability_target(self.service, parent)["scope"]}:
            raise CapabilityQueryError("INVALID_ARGUMENTS", "scope 与所选插件的归属不一致。")
        records.sort(key=lambda item: canonical_json_bytes(item["target"]))
        self.content.extend(_object(item) for item in records)
        if not records:
            self.content.append(_text("没有找到符合条件的 Hook 来源。"))

    @staticmethod
    def _component_count(component, field):
        return len(getattr(component, field)) if component.disposition.value == "COMPLETE" else 0 if component.disposition.value == "MISSING" else None

    async def _plugin_records(self, arguments):
        target = arguments.get("target")
        plugins = await self._plugins(target)
        if target is not None and not plugins:
            if self.failed:
                return
            raise CapabilityQueryError("NOT_FOUND", "未找到该插件。")
        for plugin in plugins:
            if arguments.get("scope") not in {None, plugin.identity.scope.value}:
                continue
            manifest = plugin.summary.manifest
            status = "已启用。" if plugin.enabled else "已禁用。"
            if target is None:
                self.content.append(_object({"target": plugin_target_from_inspection(plugin),
                    "name": manifest.name, "description": manifest.description or "",
                    "status": status, "components": {
                        "skills": self._component_count(plugin.summary.skills, "skills"),
                        "hooks": self._component_count(plugin.summary.hooks, "hook_definitions"),
                        "mcp_servers": self._component_count(plugin.summary.mcp, "mcp_servers")}}))
                continue
            self.content.append(_text(f"{manifest.name}：{manifest.description or ''}\n{status}"))
            groups = [
                ("SKILL", [{"kind": "SKILL", "skill_path": item.skill_path} for item in plugin.summary.skills.skills]),
                ("HOOK", [{"kind": "HOOK_SOURCE", "config_path": str(plugin_hook_config_path(plugin))}]
                    if plugin.summary.hooks.hook_definitions and plugin_hook_config_path(plugin) is not None else []),
                ("MCP", [{"kind": "MCP_CONNECTION", "config_path": str((plugin.package_root / "mcp.json").resolve(strict=False)),
                    "server_id": item.local_server_id} for item in plugin.summary.mcp.mcp_servers]),
            ]
            for label, targets in groups:
                if targets:
                    self.content.append(_text(label + "：\n"))
                    self.content.extend({"type": "target", "value": item} for item in targets)
            if not any(targets for _label, targets in groups):
                self.content.append(_text("该插件没有可查询的组件。"))
        if not self.content:
            self.content.append(_text("没有找到符合条件的插件。"))

    def _visible_runtime_servers(self, arguments):
        if self.runtime is None:
            return []
        result = []
        for server in self.runtime.catalog_for_scope(self.scope).servers:
            config = self._matching_config(server)
            if config is None or (self.scope is ModelInputScopeKind.SUBAGENT
                    and config.scope_policy is not McpScopePolicy.ROOT_AND_SUBAGENTS):
                continue
            target = server.connection_target
            if target is None:
                continue
            source = server.runtime_source
            scope = source.source_kind.value if isinstance(source, LocalConfiguredMcpRuntimeSource) else "USER" if source.store_scope_key == "user" else "WORKSPACE"
            if arguments.get("scope") not in {None, scope}:
                continue
            parent = arguments.get("parent")
            if parent is not None and (not isinstance(source, ManagedPackageMcpRuntimeSource)
                or not Path(target["config_path"]).is_relative_to(Path(parent["plugin_path"]))):
                continue
            result.append(server)
        return result

    def _matching_config(self, server):
        if not isinstance(server, McpServerCatalogEntry):
            return None
        config = self.mcp_configs.get(server.server_id)
        return config if config is not None and config.runtime_source == server.runtime_source else None

    def _service_record(self, server):
        config = self._matching_config(server)
        state = "DISABLED" if config is not None and not config.enabled else server.status.value
        status = {
            "READY": "已连接，目录已发现。",
            "DISABLED": "已配置，当前已禁用。",
            "CONNECTING": "连接中，目录未知。",
            "DISCOVERING": "正在发现目录。",
            "FAILED_RETRYABLE": "连接暂不可用。",
            "FAILED_TERMINAL": "连接失败。",
        }.get(state, state)
        record = {"target": dict(server.connection_target), "name": server.display_name, "status": status}
        if server.sanitized_instructions:
            record["instructions"] = server.sanitized_instructions
        candidate = self._accepted_directory(server.server_id) if config is not None else None
        if state == "READY" and (candidate is None or self._current_candidate(server.server_id) is None):
            record["status"] = "连接或目录暂不可用。"
        if candidate is not None:
            record["resource_count"] = len(candidate.discovery_snapshot.resources)
            record["resource_template_count"] = len(candidate.discovery_snapshot.resource_templates)
        return record

    def _accepted_directory(self, server_id):
        # The candidate carries the complete accepted discovery; the existing
        # semantic-config boundary joins it to this query's current config.
        # A physical reconnect alone does not erase that accepted knowledge.
        config = self.mcp_configs.get(server_id)
        candidate = self.runtime.candidates.get(server_id) if self.runtime is not None else None
        if (config is not None and config.enabled and candidate is not None
                and candidate.expected_semantic_config_fingerprint == config.semantic_config_fingerprint):
            return candidate
        return None

    def _current_candidate(self, server_id):
        candidate = self._accepted_directory(server_id)
        if candidate is not None:
            lease = candidate.slot_lease
            if lease._slot.lease_is_current(lease):
                return candidate
        return None

    async def _mcp_declarations(self, arguments):
        if self.service is None:
            return None
        declared = {}
        parent = arguments.get("parent")
        if parent is not None and arguments.get("scope") not in {None, resolve_capability_target(self.service, parent)["scope"]}:
            raise CapabilityQueryError("INVALID_ARGUMENTS", "scope 与所选插件的归属不一致。")

        def append(target, server_id, name, enabled):
            key = canonical_json_bytes(target)
            declared.setdefault(key, SimpleNamespace(connection_target=target, server_id=server_id,
                display_name=name, sanitized_instructions="", runtime_source=None,
                status=SimpleNamespace(value="CONNECTING" if enabled else "DISABLED")))

        if parent is None:
            from pulsara_agent.capability.mcp_management import LocalMcpTarget
            for scope in self._scopes(arguments):
                root = self.root if scope == "WORKSPACE" else None
                declarations = await self._native(self.service.mcp.inspect_declarations, root)
                for item in declarations:
                    if self.scope is ModelInputScopeKind.SUBAGENT and item.scope_policy.value != "ROOT_AND_SUBAGENTS":
                        continue
                    target = {"kind": "MCP_CONNECTION", "config_path": str(self.service.mcp.path(LocalMcpTarget(item.server_id, root))), "server_id": item.server_id}
                    append(target, item.server_id, item.display_name, item.enabled)
        component = arguments.get("target")
        for plugin in await self._plugins(parent or component):
            if arguments.get("scope") not in {None, plugin.identity.scope.value}:
                continue
            if plugin.summary.mcp.disposition.value not in {"COMPLETE", "MISSING"}:
                self._failure(f"插件 {plugin.summary.manifest.name} 的 MCP 声明未能完整读取。")
            policies = {overlay.local_server_id: overlay.scope_policy
                        for overlay in plugin.mcp_connection_overlays}
            for item in plugin.summary.mcp.mcp_servers:
                if (self.scope is ModelInputScopeKind.SUBAGENT
                    and policies.get(item.local_server_id, McpScopePolicy.ROOT_AND_SUBAGENTS)
                    is not McpScopePolicy.ROOT_AND_SUBAGENTS):
                    continue
                target = {"kind": "MCP_CONNECTION", "config_path": str((plugin.package_root / "mcp.json").resolve(strict=False)), "server_id": item.local_server_id}
                append(target, framed_plugin_mcp_server_id(plugin.identity.plugin_id, item.local_server_id),
                    f"{plugin.summary.manifest.name}:{item.local_server_id}", plugin.enabled)
        return declared

    def _mcp_servers(self, arguments, declared):
        """Join declarations to the currently accepted runtime without yielding."""
        visible = self._visible_runtime_servers(arguments)
        if declared is None:
            return visible
        servers = dict(declared)
        for server in visible:
            key = canonical_json_bytes(server.connection_target)
            if key in declared:
                servers[key] = server
        return [servers[key] for key in sorted(servers)]

    def _refresh_directory(self):
        if self.current_directory is None:
            return False
        runtime, configs = self.current_directory()
        current = {config.server_id: config for config in configs}
        changed = self.runtime is not runtime or self.mcp_configs != current
        self.runtime, self.mcp_configs = runtime, current
        return changed

    def _searchable_tools(self, servers):
        if self.runtime is None:
            return []
        ids = {
            server.server_id for server in servers
            if self._matching_config(server) is not None
            and self._accepted_directory(server.server_id) is not None
        }
        specs = (self.runtime.root_tool_specs if self.scope is ModelInputScopeKind.ROOT
                 else self.runtime.subagent_tool_specs)
        return [tool for tool in specs if tool.server_id in ids]

    async def _mcp_keyword_matches(self, arguments, declared):
        from pulsara_agent.capability.mcp_search import mcp_tool_search_text, search_mcp

        query = arguments["query"]
        self._refresh_directory()
        servers = self._mcp_servers(arguments, declared)
        tools = self._searchable_tools(servers)
        embeddings = self.tool_embeddings
        connection = embeddings.current_connection() if embeddings is not None and tools else None
        similarities = {}
        if connection is not None:
            # Plugin inspection pins package contents. Only the text is needed
            # across network I/O. Reopen the declarations for ranking.
            self._close_inspections()
            scores = await embeddings.scores(
                query,
                connection=connection,
                texts=[mcp_tool_search_text(tool) for tool in tools],
                deadline_monotonic=self.deadline,
            )
            self._check()
            self.content.clear()
            self.failed = False
            declared = await self._mcp_declarations(arguments)
            self._refresh_directory()
            servers = self._mcp_servers(arguments, declared)
            tools = self._searchable_tools(servers)
            if scores and embeddings.current_connection() == connection:
                similarities = {
                    (tool.server_id, tool.remote_tool_name): scores[text]
                    for tool in tools if (text := mcp_tool_search_text(tool)) in scores
                }
        ranked_configs = self.mcp_configs
        matches = await self._native(
            search_mcp, query, tools=tools, servers=servers, dense_similarities=similarities
        )
        await self._capture_scrub()
        if similarities and embeddings.current_connection() != connection:
            self._refresh_directory()
            servers = self._mcp_servers(arguments, declared)
            tools = self._searchable_tools(servers)
            ranked_configs = self.mcp_configs
            matches = await self._native(search_mcp, query, tools=tools, servers=servers)
            await self._capture_scrub()
        if self._refresh_directory():
            servers = self._mcp_servers(arguments, declared)
            matches = self._confirmed_mcp_matches(query, matches, servers, ranked_configs)
            self._failure("MCP 目录在搜索期间更新；本次搜索不完整，仅返回仍可确认的匹配。")
        return matches, servers

    def _confirmed_mcp_matches(self, query, matches, servers, ranked_configs):
        from pulsara_agent.capability.mcp_search import mcp_tool_description_excerpt

        # Keep confirmed matches when the directory moves again during ranking.
        # Result construction reads the current schema after all awaited work.
        by_target = {canonical_json_bytes(server.connection_target): server for server in servers}
        current_tools = self._searchable_tools(servers)
        tools = {
            (tool.server_id, tool.remote_tool_name): tool
            for tool in current_tools
            if (previous := ranked_configs.get(tool.server_id)) is not None
            and previous.runtime_source == self.mcp_configs[tool.server_id].runtime_source
        }
        confirmed = [
            ("tool", tool, mcp_tool_description_excerpt(tool.description))
            for tool in current_tools if tool.remote_tool_name == query
        ]
        for kind, item, excerpt in matches:
            if kind == "tool":
                tool = tools.get((item.server_id, item.remote_tool_name))
                if tool is not None and tool.remote_tool_name != query and tool.description == item.description:
                    confirmed.append((kind, tool, excerpt))
            else:
                server = by_target.get(canonical_json_bytes(item.connection_target))
                if (server is not None and server.server_id == item.server_id
                    and server.runtime_source == item.runtime_source
                    and server.display_name == item.display_name
                    and server.sanitized_instructions == item.sanitized_instructions):
                    confirmed.append((kind, server, excerpt))
        return confirmed

    async def _mcp(self, arguments):
        target = arguments.get("target")
        declared = (None if target is not None and target["kind"] == "MCP_TOOL"
            else await self._mcp_declarations(arguments))
        if "query" in arguments:
            matches, servers = await self._mcp_keyword_matches(arguments, declared)
        else:
            await self._capture_scrub()
            self._refresh_directory()
            servers = self._mcp_servers(arguments, declared)
        if target is None and "query" not in arguments:
            self.content.extend(_object(self._service_record(server)) for server in servers)
            if not servers:
                self.content.append(_text("没有找到符合条件的 MCP 服务。"))
            return
        if target is not None and target["kind"] == "MCP_CONNECTION":
            servers = [server for server in servers if dict(server.connection_target) == target]
            if not servers:
                if self.failed:
                    return
                raise CapabilityQueryError("NOT_FOUND", "未找到当前范围内的 MCP 连接。")
            server = servers[0]
            candidate = self._current_candidate(server.server_id) if self._matching_config(server) is not None else None
            if candidate is None:
                raise CapabilityQueryError("DIRECTORY_UNAVAILABLE", "该服务当前目录未知或不可读取。")
            specs = self.runtime.root_tool_specs if self.scope is ModelInputScopeKind.ROOT else self.runtime.subagent_tool_specs
            tools = [tool for tool in specs if tool.server_id == server.server_id]
            self.content.extend(_object({"target": {"kind": "MCP_TOOL", "server_id": tool.server_id,
                "remote_tool_name": tool.remote_tool_name}, "description": tool.description,
                "status": "可通过 call_mcp_tool 调用；参数请查询工具详情。"}) for tool in tools)
            if not tools:
                self.content.append(_text("该服务当前没有可见工具。"))
            return
        if target is not None:
            tools = [tool for tool in self._searchable_tools(servers)
                if tool.server_id == target["server_id"] and tool.remote_tool_name == target["remote_tool_name"]]
            if not tools:
                raise CapabilityQueryError("NOT_FOUND", "未找到当前范围内的 MCP 工具。")
            executor = (self.runtime.executors.get((tools[0].server_id, tools[0].remote_tool_name))
                if self._current_candidate(tools[0].server_id) is not None else None)
            if executor is None:
                self.content.append(_object({"target": target, "description": tools[0].description, "invocation": {"mode": "UNAVAILABLE"}}))
                self.content.append(_text("该工具当前没有可用执行连接。"))
            else:
                self.content.append(_object(self.inspect_tool(executor)))
            return
        for kind, item, description in matches:
            if kind == "service":
                self.content.append(_object(self._service_record(item)))
            else:
                executor = (self.runtime.executors.get((item.server_id, item.remote_tool_name))
                    if self._current_candidate(item.server_id) is not None else None)
                if executor is not None:
                    record = self.inspect_tool(executor)
                else:
                    record = {"target": {"kind": "MCP_TOOL", "server_id": item.server_id, "remote_tool_name": item.remote_tool_name},
                              "invocation": {"mode": "UNAVAILABLE"}}
                if description:
                    record["description"] = description
                else:
                    record.pop("description", None)
                self.content.append(_object(record))
        incomplete = [server for server in servers
            if ((config.enabled if (config := self._matching_config(server)) is not None
                else server.status.value != "DISABLED")
                and (config is None or self._accepted_directory(server.server_id) is None))]
        if incomplete:
            names = []
            for server in incomplete:
                origin = server.runtime_source
                suffix = (f"（来自插件 {origin.package_owner_key}）"
                    if isinstance(origin, ManagedPackageMcpRuntimeSource) else "")
                names.append(server.connection_target["server_id"] + suffix)
            self._failure(
                ("已返回已知匹配；" if matches else "在已知目录中未找到匹配；")
                + "以下已启用服务的工具目录尚未取得或无法可靠读取："
                + "、".join(names) + "。本次搜索不完整，不能确认没有其他合适工具。可使用已返回的可用接口，或按需再次查询。")
        elif not matches and not self.failed:
            self.content.append(_text("没有找到匹配的 MCP 工具或服务。"))

    def _close_inspections(self):
        while self.held:
            self.held.pop().close()

    async def _capture_scrub(self):
        if self.service is not None:
            self._scrub = await asyncio.to_thread(self.service.plugins._capture_scrub_set,
                InspectLocalPluginsRequest(self.deadline))

    async def execute(self, arguments):
        status = "SUCCESS"
        try:
            values = parse_query(arguments, subagent=self.scope is ModelInputScopeKind.SUBAGENT)
            kind = values["kind"]
            if kind == "MCP":
                await self._mcp(values)
            elif kind == "SKILL":
                await self._skills(values)
            elif kind == "HOOK":
                await self._hooks(values)
            else:
                await self._plugin_records(values)
            if self.failed:
                status = "ERROR"
        except asyncio.CancelledError:
            status = "CANCELLED"
            self.content.insert(0, _text("能力查询已取消。"))
        except (CapabilityQueryError, ValueError, OSError) as exc:
            status = "ERROR"
            self.content.insert(0, _text(str(exc)))
        finally:
            self._close_inspections()
        result = {"meta": {"status": status}, "content": self.content}
        if self._scrub is None:
            await self._capture_scrub()
        return self._scrub.scrub_json(result) if self._scrub is not None else result
