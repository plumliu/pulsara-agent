"""Cold-safe Session control plane for the local Web application."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Literal, Mapping
from uuid import uuid4

from pulsara_agent.capability.local_skill_management import (
    InstallLooseLocalSkillRequest,
    LocalSkillInstallOutcome,
    LocalSkillInstallScope,
    LocalSkillManagementService,
)
from pulsara_agent.capability.contracts import LocalSkillRootKind
from pulsara_agent.capability.local_skills import (
    LooseSkillDefinitionProducer,
    LooseSkillDefinitionsDisposition,
)
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.capability.resolver import (
    CompleteEffectiveSkillCatalogInspection,
    UnavailableEffectiveSkillCatalogInspection,
)
from pulsara_agent.capability.types import (
    ConflictingSkillCandidateIssue,
    InvalidSkillCandidateIssue,
    LooseSkillOrigin,
    ProducerUnavailableCause,
    ResolutionUnavailableCause,
    ShadowedSkillCandidateIssue,
)
from pulsara_agent.capability.user_skill_config import (
    load_user_skill_config,
    set_user_skill_enabled as write_user_skill_enabled,
)
from pulsara_agent.conversation_kernel.host import (
    KernelCapabilityCatalogInspection,
    KernelHostCore,
    KernelHostCoreClosing,
    KernelHostSession,
    KernelSessionSummary,
)
from pulsara_agent.llm.models import ModelRole
from pulsara_agent.mcp_config import (
    DEFAULT_USER_MCP_CONFIG,
    McpLocalConfigSourceKind,
    McpScopePolicy,
    McpServerConfig,
    StdioTransportConfig,
    StreamableHttpTransportConfig,
    load_mcp_server_configs,
    set_mcp_server_enabled,
    write_mcp_server_config,
)
from pulsara_agent.plugins.contracts import (
    AlreadyPresentPluginInstallOutcome,
    CleanupUnavailablePluginInstallOutcome,
    FailedPluginEnablementOutcome,
    FailedPluginInstallOutcome,
    FailedPluginRemovalOutcome,
    PluginInspectionAbort,
    PluginInspectionDisposition,
    PluginInspectionOutcome,
    PluginScopeKind,
    RemovedPluginOutcome,
    SettledPluginEnablementOutcome,
    SuccessfulPluginInstallOutcome,
)
from pulsara_agent.tool_permission import EffectivePermissionPolicy
from pulsara_agent.workspace_identity import HostWorkspaceInput, resolve_workspace


SessionWorkspaceKind = Literal["quick", "project"]
_TASK_PAGE_MAXIMUM = 50
_TASK_CURSOR_MAXIMUM_BYTES = 2048


@dataclass(frozen=True, slots=True)
class HostSessionHandle:
    """Published live Session plus creator-held disposal capability."""

    session: KernelHostSession
    workspace_input: HostWorkspaceInput

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def host_session_id(self) -> str:
        return self.session.host_session_id


def _display_session_id(session_id: str) -> str:
    suffix = session_id.removeprefix("session:")
    return suffix[:8] if suffix else session_id[:8]


def _create_quick_workspace_root(managed_root: Path, now: datetime) -> Path:
    """Create one short, readable, process-safe managed workspace directory."""

    stem = f"quick-{now.strftime('%Y%m%d-%H%M%S')}"
    while True:
        candidate = managed_root / f"{stem}-{uuid4().hex[:8]}"
        try:
            candidate.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            continue
        return candidate


class LocalSessionController:
    """Own create/resume publication while preserving Kernel close authority."""

    def __init__(
        self,
        *,
        core: KernelHostCore,
        workspace_input: HostWorkspaceInput,
        model_role: ModelRole,
        permission_policy: EffectivePermissionPolicy,
        active_skill_names: frozenset[str],
    ) -> None:
        self.core = core
        self.workspace_input = workspace_input
        self.model_role = model_role
        self.permission_policy = permission_policy
        self.active_skill_names = active_skill_names
        self._by_session: dict[str, HostSessionHandle] = {}
        self._by_host: dict[str, HostSessionHandle] = {}
        self._resumes: dict[str, asyncio.Task[HostSessionHandle]] = {}
        self._lock = asyncio.Lock()
        self._capability_mutation_lock = asyncio.Lock()
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    async def prepare(self) -> list[dict[str, object]]:
        """Verify cold resources without activating a HostSession."""

        return await self.list_sessions()

    def bootstrap_payload(self) -> dict[str, object]:
        workspace = resolve_workspace(self.workspace_input)
        return {
            "application": {
                "name": "Pulsara",
                "version": "0.1.0",
                "transport": "localhost-http+terminal-v3",
            },
            "workspace": {
                "id": workspace.workspace_key,
                "name": Path(workspace.workspace_root).name or "workspace",
                "path": str(workspace.workspace_root),
                "kind": workspace.workspace_kind,
            },
            "provider": self.core.settings.redacted_dict()["llm"],
            "storage": self.core.settings.redacted_dict()["storage"],
            "protocol": {"major": 3, "minor": 0},
        }

    async def list_sessions(self) -> list[dict[str, object]]:
        summaries = await self.core.list_resumable_sessions_across_workspaces(
            memory_domain_id=self.workspace_input.memory_domain_id,
            include_closed=False,
        )
        async with self._lock:
            live_ids = frozenset(self._by_session)
        return [
            self._summary_payload(item, item.session_id in live_ids)
            for item in summaries
        ]

    async def list_session_tasks(
        self,
        session_id: str,
        *,
        maximum_items: int = _TASK_PAGE_MAXIMUM,
        cursor: str | None = None,
    ) -> dict[str, object]:
        """Cold-read one session's complete durable task inventory by page."""

        if not 1 <= maximum_items <= _TASK_PAGE_MAXIMUM:
            raise ValueError("task page size is out of bounds")
        summary = await self.core.read_resumable_session(
            session_id,
            memory_domain_id=self.workspace_input.memory_domain_id,
        )
        if summary is None:
            raise KeyError(session_id)
        after_accepted_at: datetime | None = None
        after_task_id: str | None = None
        seen_count = 0
        if cursor is not None:
            after_accepted_at, after_task_id, seen_count = _decode_task_cursor(
                cursor,
                session_id=session_id,
            )
        durable, dependency_rows = await self.core.read_subagent_task_page(
            session_id=session_id,
            maximum_items=maximum_items,
            after_accepted_at=after_accepted_at,
            after_task_id=after_task_id,
        )
        has_more = len(durable) > maximum_items
        page = durable[:maximum_items]
        dependencies: dict[str, list[dict[str, object]]] = {
            str(item["id"]): [] for item in page
        }
        for edge in dependency_rows:
            dependencies[str(edge["task_id"])].append(
                {
                    "task_id": str(edge["dependency_task_id"]),
                    "task_key": edge.get("task_key"),
                    "label": edge.get("label"),
                    "status": str(edge["status"]),
                    "result_id": edge.get("result_id"),
                    "result_source": edge.get("result_source"),
                    "result_summary": edge.get("summary"),
                }
            )
        tasks = [_task_payload(item, dependencies[str(item["id"])]) for item in page]
        total_count = int(page[0]["total_count"]) if page else seen_count
        next_seen_count = seen_count + len(page)
        next_cursor: str | None = None
        if has_more:
            last = page[-1]
            accepted_at = last["accepted_at"]
            if not isinstance(accepted_at, datetime):
                raise RuntimeError("task page lacks its ordering timestamp")
            next_cursor = _encode_task_cursor(
                session_id=session_id,
                accepted_at=accepted_at,
                task_id=str(last["id"]),
                seen_count=next_seen_count,
            )
        return {
            "session_id": session_id,
            "tasks": tasks,
            "total_count": total_count,
            "page_count": len(tasks),
            "remaining_count": max(0, total_count - next_seen_count),
            "next_cursor": next_cursor,
        }

    async def inspect_session_capabilities(self, session_id: str) -> dict[str, object]:
        """Project current capability owners for one selected Session."""

        handle = await self.resume_session(session_id)
        return _capability_payload(
            session_id=session_id,
            workspace_root=handle.session.workspace.workspace_root,
            inspection=handle.session.inspect_capability_catalog(),
        )

    async def reconnect_session_mcp(
        self, session_id: str, server_id: str
    ) -> dict[str, object]:
        if not server_id:
            raise ValueError("MCP server id is required")
        handle = await self.resume_session(session_id)
        handle.session.reconnect_mcp_server(server_id)
        return _capability_payload(
            session_id=session_id,
            workspace_root=handle.session.workspace.workspace_root,
            inspection=handle.session.inspect_capability_catalog(),
        )

    async def install_session_skill(
        self,
        session_id: str,
        *,
        source_path: str,
        scope: str,
    ) -> dict[str, object]:
        if scope not in {"workspace", "user"}:
            raise ValueError("Skill installation scope is invalid")
        source = Path(source_path.strip()).expanduser()
        if not source_path.strip() or not source.is_absolute():
            raise ValueError("Skill source path must be absolute")
        handle = await self.resume_session(session_id)
        install_scope = LocalSkillInstallScope(scope)
        workspace_root = (
            handle.session.workspace.workspace_root
            if install_scope is LocalSkillInstallScope.WORKSPACE
            else None
        )
        outcome = await asyncio.to_thread(
            LocalSkillManagementService().install_loose_local_skill,
            InstallLooseLocalSkillRequest(
                source_path=source,
                scope=install_scope,
                workspace_root=workspace_root,
            ),
        )
        return {
            "installation": _skill_install_payload(outcome),
            "capabilities": _capability_payload(
                session_id=session_id,
                workspace_root=handle.session.workspace.workspace_root,
                inspection=handle.session.inspect_capability_catalog(),
            ),
        }

    async def inspect_user_capabilities(
        self, *, active_session_id: str | None = None
    ) -> dict[str, object]:
        """Read the user-owned Skill, MCP, and Plugin inventory."""

        workspace_root = resolve_workspace(self.workspace_input).workspace_root
        skills_task = asyncio.to_thread(_inspect_user_skills, workspace_root)
        mcp_task = asyncio.to_thread(load_mcp_server_configs)
        plugins_task = self.core.inspect_user_plugins()
        skills, mcp_configs, plugins = await asyncio.gather(
            skills_task, mcp_task, plugins_task
        )
        live_inspection: KernelCapabilityCatalogInspection | None = None
        if active_session_id:
            async with self._lock:
                handle = self._by_session.get(active_session_id)
                if handle is not None:
                    live_inspection = handle.session.inspect_capability_catalog()
        try:
            return _user_capability_payload(
                skills=skills,
                mcp_configs=mcp_configs,
                plugins=plugins,
                live_inspection=live_inspection,
            )
        finally:
            if isinstance(plugins, PluginInspectionOutcome):
                plugins.close()

    async def refresh_user_capabilities(
        self, *, active_session_id: str | None = None
    ) -> dict[str, object]:
        """Adopt current user sources in every live Session, then inspect them."""

        async with self._capability_mutation_lock:
            updated, attention = await self._reload_live_capabilities()
            payload = await self.inspect_user_capabilities(
                active_session_id=active_session_id
            )
        payload["adoption"] = {
            "updated_sessions": updated,
            "attention_sessions": attention,
        }
        return payload

    async def install_user_skill(
        self, *, source_path: str, active_session_id: str | None = None
    ) -> dict[str, object]:
        source = _absolute_local_source(source_path, "Skill")
        async with self._capability_mutation_lock:
            outcome = await asyncio.to_thread(
                LocalSkillManagementService().install_loose_local_skill,
                InstallLooseLocalSkillRequest(
                    source_path=source,
                    scope=LocalSkillInstallScope.USER,
                ),
            )
            capabilities = await self.inspect_user_capabilities(
                active_session_id=active_session_id
            )
        return {
            "operation": _skill_install_payload(outcome),
            "capabilities": capabilities,
        }

    async def set_user_skill_enabled(
        self,
        *,
        skill_path: str,
        enabled: bool,
        active_session_id: str | None = None,
    ) -> dict[str, object]:
        requested = Path(skill_path.strip()).expanduser()
        if not skill_path.strip() or not requested.is_absolute():
            raise ValueError("Skill path must be absolute")
        workspace_root = resolve_workspace(self.workspace_input).workspace_root
        async with self._capability_mutation_lock:
            observed = await asyncio.to_thread(_inspect_user_skills, workspace_root)
            requested = requested.resolve(strict=False)
            raw_items = observed.get("items")
            if not isinstance(raw_items, list):
                raise RuntimeError("user Skill inventory is invalid")
            known_paths = set()
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                item_path = item.get("path")
                if isinstance(item_path, str):
                    known_paths.add(Path(item_path).resolve(strict=False))
            if requested not in known_paths:
                raise ValueError("Skill is not in a user capability directory")
            await asyncio.to_thread(
                write_user_skill_enabled,
                skill_path=requested,
                enabled=enabled,
            )
            capabilities = await self.inspect_user_capabilities(
                active_session_id=active_session_id
            )
        return {
            "operation": {
                "status": "ENABLED" if enabled else "DISABLED",
                "success": True,
                "message": "技能已开启。" if enabled else "技能已关闭。",
            },
            "capabilities": capabilities,
        }

    async def create_user_mcp_server(
        self,
        *,
        server_id: str,
        display_name: str,
        transport: str,
        endpoint: str | None,
        command: str | None,
        args: list[str],
        available_to_subagents: bool,
        active_session_id: str | None = None,
    ) -> dict[str, object]:
        server_id = server_id.strip()
        display_name = display_name.strip() or server_id
        if transport == "http":
            entry: dict[str, object] = {
                "display_name": display_name,
                "enabled": True,
                "required": False,
                "transport": "streamable_http",
                "url": (endpoint or "").strip(),
                "scope_policy": (
                    "ROOT_AND_SUBAGENTS" if available_to_subagents else "ROOT_ONLY"
                ),
            }
        elif transport == "stdio":
            entry = {
                "display_name": display_name,
                "enabled": True,
                "required": False,
                "transport": "stdio",
                "command": (command or "").strip(),
                "args": args,
                "scope_policy": (
                    "ROOT_AND_SUBAGENTS" if available_to_subagents else "ROOT_ONLY"
                ),
            }
        else:
            raise ValueError("MCP transport is invalid")
        async with self._capability_mutation_lock:
            existing = await asyncio.to_thread(load_mcp_server_configs)
            if any(item.server_id == server_id for item in existing):
                raise ValueError("MCP server id already exists")
            await asyncio.to_thread(
                write_mcp_server_config,
                server_id=server_id,
                entry=entry,
            )
            updated, attention = await self._reload_live_capabilities()
            capabilities = await self.inspect_user_capabilities(
                active_session_id=active_session_id
            )
        return {
            "operation": {
                "status": "ADDED",
                "success": True,
                "message": "MCP 服务已添加，并已用于当前打开的会话。",
            },
            "adoption": {
                "updated_sessions": updated,
                "attention_sessions": attention,
            },
            "capabilities": capabilities,
        }

    async def set_user_mcp_enabled(
        self,
        *,
        server_id: str,
        enabled: bool,
        active_session_id: str | None = None,
    ) -> dict[str, object]:
        async with self._capability_mutation_lock:
            await asyncio.to_thread(
                set_mcp_server_enabled,
                server_id=server_id,
                enabled=enabled,
            )
            updated, attention = await self._reload_live_capabilities()
            capabilities = await self.inspect_user_capabilities(
                active_session_id=active_session_id
            )
        return {
            "operation": {
                "status": "ENABLED" if enabled else "DISABLED",
                "success": True,
                "message": "MCP 服务已开启。" if enabled else "MCP 服务已关闭。",
            },
            "adoption": {
                "updated_sessions": updated,
                "attention_sessions": attention,
            },
            "capabilities": capabilities,
        }

    async def install_user_plugin(
        self, *, source_path: str, active_session_id: str | None = None
    ) -> dict[str, object]:
        source = _absolute_local_source(source_path, "Plugin")
        async with self._capability_mutation_lock:
            outcome = await self.core.install_user_plugin(source)
            capabilities = await self.inspect_user_capabilities(
                active_session_id=active_session_id
            )
        return {
            "operation": _plugin_install_operation(outcome),
            "capabilities": capabilities,
        }

    async def set_user_plugin_enabled(
        self,
        *,
        plugin_id: str,
        package_install_id: str,
        enabled: bool,
        active_session_id: str | None = None,
    ) -> dict[str, object]:
        async with self._capability_mutation_lock:
            outcome = await self.core.set_user_plugin_enabled(
                plugin_id=plugin_id,
                package_install_id=package_install_id,
                enabled=enabled,
            )
            updated, attention = await self._reload_live_capabilities()
            capabilities = await self.inspect_user_capabilities(
                active_session_id=active_session_id
            )
        return {
            "operation": _plugin_enablement_operation(outcome),
            "adoption": {
                "updated_sessions": updated,
                "attention_sessions": attention,
            },
            "capabilities": capabilities,
        }

    async def remove_user_plugin(
        self, *, plugin_id: str, active_session_id: str | None = None
    ) -> dict[str, object]:
        async with self._capability_mutation_lock:
            outcome = await self.core.remove_user_plugin(plugin_id)
            updated, attention = await self._reload_live_capabilities()
            capabilities = await self.inspect_user_capabilities(
                active_session_id=active_session_id
            )
        return {
            "operation": _plugin_removal_operation(outcome),
            "adoption": {
                "updated_sessions": updated,
                "attention_sessions": attention,
            },
            "capabilities": capabilities,
        }

    async def open_capability_root(self, root: str) -> dict[str, object]:
        if root == "agents":
            path = Path.home() / ".agents"
        elif root == "pulsara":
            path = require_pulsara_home()
        else:
            raise ValueError("unknown capability root")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if sys.platform != "darwin":
            raise RuntimeError("directory reveal is unavailable")
        process = await asyncio.create_subprocess_exec("open", str(path))
        status = await process.wait()
        if status != 0:
            raise RuntimeError("directory reveal failed")
        return {"opened": True, "path": str(path)}

    async def _reload_live_capabilities(self) -> tuple[int, int]:
        async with self._lock:
            handles = tuple(self._by_session.values())
        if not handles:
            return 0, 0
        results = await asyncio.gather(
            *(handle.session.reload_plugins(deadline_monotonic=None) for handle in handles),
            return_exceptions=True,
        )
        attention = sum(
            1
            for result in results
            if isinstance(result, BaseException)
            or (isinstance(result, dict) and result.get("status") != "RELOADED")
        )
        return len(results) - attention, attention

    async def create_session(
        self,
        *,
        workspace_kind: SessionWorkspaceKind,
        workspace_path: str | None = None,
    ) -> HostSessionHandle:
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Local Web application is draining")
        workspace_input = self._workspace_input_for_create(
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
        )
        session = await self.core.open_session(
            workspace_input,
            model_role=self.model_role,
            permission_policy=self.permission_policy,
            active_skill_names=self.active_skill_names,
        )
        handle = HostSessionHandle(session, workspace_input)
        try:
            async with self._lock:
                if self._closing:
                    raise KernelHostCoreClosing("Local Web application is draining")
                self._publish_locked(handle)
            return handle
        except BaseException:
            await self.core.close_session(
                session.host_session_id, close_conversation=False
            )
            raise

    async def resume_session(self, session_id: str) -> HostSessionHandle:
        if not session_id:
            raise ValueError("session_id is required")
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Local Web application is draining")
            live = self._by_session.get(session_id)
            if live is not None:
                return live
            task = self._resumes.get(session_id)
            if task is None:
                task = asyncio.create_task(
                    self._resume_owner(session_id),
                    name=f"local-web-resume:{session_id}",
                )
                self._resumes[session_id] = task
        return await asyncio.shield(task)

    async def _resume_owner(self, session_id: str) -> HostSessionHandle:
        task = asyncio.current_task()
        try:
            summary = await self.core.read_resumable_session(
                session_id,
                memory_domain_id=self.workspace_input.memory_domain_id,
            )
            if summary is None:
                raise KeyError(session_id)
            workspace_input = HostWorkspaceInput(
                workspace_kind=(
                    "project" if summary.workspace_kind == "project" else "transient"
                ),
                workspace_root=summary.workspace_root,
                display_label=summary.workspace_label,
                memory_domain_id=summary.memory_domain_id,
                cleanup_workspace_root_on_close=False,
                trust_workspace_mcp_config=(
                    self.workspace_input.trust_workspace_mcp_config
                ),
            )
            session = await self.core.resume_session(
                session_id,
                workspace_input=workspace_input,
                model_role=self.model_role,
                permission_policy=self.permission_policy,
                active_skill_names=self.active_skill_names,
            )
            handle = HostSessionHandle(session, workspace_input)
            try:
                raced: HostSessionHandle | None = None
                async with self._lock:
                    if self._closing:
                        raise KernelHostCoreClosing("Local Web application is draining")
                    raced = self._by_session.get(session_id)
                    if raced is None:
                        self._publish_locked(handle)
                if raced is not None:
                    await self.core.close_session(
                        session.host_session_id, close_conversation=False
                    )
                    return raced
                return handle
            except BaseException:
                await self.core.close_session(
                    session.host_session_id, close_conversation=False
                )
                raise
        finally:
            async with self._lock:
                if self._resumes.get(session_id) is task:
                    self._resumes.pop(session_id, None)

    def _publish_locked(self, handle: HostSessionHandle) -> None:
        if handle.session_id in self._by_session:
            raise RuntimeError("canonical Session already has a live local owner")
        if handle.host_session_id in self._by_host:
            raise RuntimeError("HostSession identity collision")
        self._by_session[handle.session_id] = handle
        self._by_host[handle.host_session_id] = handle

    def _workspace_input_for_create(
        self,
        *,
        workspace_kind: SessionWorkspaceKind,
        workspace_path: str | None,
    ) -> HostWorkspaceInput:
        if workspace_kind == "quick":
            if workspace_path is not None:
                raise ValueError("quick start does not accept workspace_path")
            managed_root = require_pulsara_home() / "workspaces"
            managed_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            now = datetime.now().astimezone()
            root = _create_quick_workspace_root(managed_root, now)
            workspace_input = HostWorkspaceInput(
                workspace_kind="transient",
                workspace_root=root,
                display_label=f"快速开始 · {now.strftime('%m月%d日 %H:%M')}",
                memory_domain_id=self.workspace_input.memory_domain_id,
                cleanup_workspace_root_on_close=False,
                trust_workspace_mcp_config=False,
            )
        elif workspace_kind == "project":
            if workspace_path is None or not workspace_path.strip():
                raise ValueError("project workspace_path is required")
            candidate = Path(workspace_path.strip()).expanduser()
            if not candidate.is_absolute():
                raise ValueError("project workspace_path must be absolute")
            workspace_input = HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=candidate,
                display_label=None,
                memory_domain_id=self.workspace_input.memory_domain_id,
                cleanup_workspace_root_on_close=False,
                trust_workspace_mcp_config=(
                    self.workspace_input.trust_workspace_mcp_config
                ),
            )
        else:
            raise ValueError("workspace_kind must be quick or project")
        # Resolve before Host activation so all invalid paths fail before any
        # canonical Session row can be admitted.
        resolve_workspace(workspace_input)
        return workspace_input

    def session_by_host_id(self, host_session_id: str) -> KernelHostSession:
        handle = self._by_host.get(host_session_id)
        if handle is None:
            raise KeyError(host_session_id)
        return handle.session

    async def close_session(self, session_id: str, *, close_conversation: bool) -> None:
        async with self._lock:
            handle = self._by_session.pop(session_id, None)
            if handle is not None:
                self._by_host.pop(handle.host_session_id, None)
        if handle is None:
            if close_conversation:
                # Canonical close is only legal through a live writer owner.
                handle = await self.resume_session(session_id)
                async with self._lock:
                    self._by_session.pop(session_id, None)
                    self._by_host.pop(handle.host_session_id, None)
            else:
                return
        await self.core.close_session(
            handle.host_session_id, close_conversation=close_conversation
        )

    async def aclose(self) -> None:
        task = self._close_task
        if task is None:
            task = asyncio.create_task(
                self._close_owner(), name="local-session-controller-close"
            )
            self._close_task = task
        await asyncio.shield(task)

    async def _close_owner(self) -> None:
        async with self._lock:
            self._closing = True
            resumes = tuple(self._resumes.values())
        if resumes:
            await asyncio.gather(
                *(asyncio.shield(task) for task in resumes), return_exceptions=True
            )
        async with self._lock:
            handles = tuple(self._by_session.values())
            self._by_session.clear()
            self._by_host.clear()
        for handle in handles:
            await self.core.close_session(
                handle.host_session_id, close_conversation=False
            )

    @staticmethod
    def _summary_payload(
        summary: KernelSessionSummary, live: bool
    ) -> dict[str, object]:
        updated = summary.updated_at
        if isinstance(updated, datetime):
            updated_value = updated.isoformat()
        else:
            updated_value = str(updated)
        return {
            "id": summary.session_id,
            "title": f"会话 {_display_session_id(summary.session_id)}",
            "subtitle": f"{summary.latest_entry_sequence} 条记录",
            "lifecycle": summary.lifecycle,
            "status": "waiting" if live else "completed",
            "updated_at": updated_value,
            "latest_entry_sequence": summary.latest_entry_sequence,
            "writer_generation": summary.writer_generation,
            "live": live,
            "task_counts": {
                "total": summary.subagent_task_total,
                "active": summary.subagent_task_active,
                "waiting": summary.subagent_task_waiting,
                "attention": summary.subagent_task_attention,
            },
            "workspace": {
                "id": summary.workspace_id,
                "name": summary.workspace_label,
                "path": summary.workspace_root,
                "kind": (
                    "quick" if summary.workspace_kind == "transient" else "project"
                ),
            },
        }


_USER_SKILL_ROOTS = frozenset(
    {LocalSkillRootKind.USER_PULSARA, LocalSkillRootKind.USER_AGENTS}
)


def _absolute_local_source(value: str, label: str) -> Path:
    source = Path(value.strip()).expanduser()
    if not value.strip() or not source.is_absolute():
        raise ValueError(f"{label} source path must be absolute")
    return source


def _inspect_user_skills(workspace_root: Path) -> dict[str, object]:
    producer = LooseSkillDefinitionProducer()
    config = load_user_skill_config()
    policy = producer.prepare_root_policy(workspace_root)
    observed = producer.observe(policy)
    roots = [
        {
            "kind": (
                "pulsara"
                if item.root_kind is LocalSkillRootKind.USER_PULSARA
                else "agents"
            ),
            "path": str(item.path),
        }
        for item in policy.roots
        if item.root_kind in _USER_SKILL_ROOTS
    ]
    if observed.disposition is LooseSkillDefinitionsDisposition.UNAVAILABLE:
        cause = observed.unavailable_cause
        return {
            "status": "attention",
            "items": [],
            "issues": [],
            "details": [
                *(
                    [item.message for item in cause.diagnostics]
                    if cause is not None
                    else []
                ),
                *([config.error] if config.error is not None else []),
            ],
            "roots": roots,
            "config_path": str(config.config_path),
        }
    items = []
    for item in observed.candidates:
        origin = item.origin
        if not isinstance(origin, LooseSkillOrigin) or origin.root_kind not in _USER_SKILL_ROOTS:
            continue
        items.append(
            {
                "name": item.name,
                "description": item.description,
                "location": item.location,
                "path": str(item.path),
                "enabled": config.enabled_for(item.path),
                "root": (
                    "pulsara"
                    if origin.root_kind is LocalSkillRootKind.USER_PULSARA
                    else "agents"
                ),
                "authoring_notes": [
                    code.value for code in item.authoring_diagnostic_codes
                ],
            }
        )
    issues = []
    for issue in observed.invalid_issues:
        origin = issue.origin
        if not isinstance(origin, LooseSkillOrigin) or origin.root_kind not in _USER_SKILL_ROOTS:
            continue
        issues.append(
            {
                "kind": "invalid",
                "title": "有一个技能没有通过检查",
                "path": str(issue.path),
                "details": [item.message for item in issue.diagnostics],
            }
        )
    return {
        "status": "ready" if config.available else "attention",
        "items": sorted(items, key=lambda item: (str(item["name"]), str(item["path"]))),
        "issues": issues,
        "details": [config.error] if config.error is not None else [],
        "roots": roots,
        "config_path": str(config.config_path),
    }


def _user_capability_payload(
    *,
    skills: dict[str, object],
    mcp_configs: tuple[McpServerConfig, ...],
    plugins: object,
    live_inspection: KernelCapabilityCatalogInspection | None,
) -> dict[str, object]:
    live_catalog = (
        {item.server_id: item for item in live_inspection.mcp_catalog.servers}
        if live_inspection is not None
        else {}
    )
    live_tools: dict[str, list[dict[str, object]]] = {}
    live_configs = (
        {
            item.server_id: item
            for item in live_inspection.mcp_configured_servers
        }
        if live_inspection is not None
        else {}
    )
    if live_inspection is not None:
        for item in live_inspection.mcp_tools:
            live_tools.setdefault(item.server_id, []).append(
                {
                    "name": item.provider_name,
                    "remote_name": item.remote_name,
                    "description": item.description,
                    "effect": item.effect,
                    "available_to_subagents": item.available_to_subagents,
                    "parallel_safe": item.parallel_safe,
                }
            )
    mcp_servers = []
    for config in mcp_configs:
        live_config = live_configs.get(config.server_id)
        exact_user_config = (
            live_config is not None
            and live_config.status_matches_config
            and live_config.source_kind == McpLocalConfigSourceKind.USER.value
            and live_config.resolved_config_identity
            == config.resolved_config_identity
        )
        live = live_catalog.get(config.server_id) if exact_user_config else None
        if not config.enabled:
            status = "DISABLED"
        elif live is None:
            status = "CONFIGURED"
        else:
            status = live.status.value
        if isinstance(config.transport, StdioTransportConfig):
            transport = {
                "kind": "stdio",
                "summary": config.transport.command,
            }
        elif isinstance(config.transport, StreamableHttpTransportConfig):
            transport = {
                "kind": "http",
                "summary": config.transport.endpoint,
            }
        else:  # pragma: no cover - closed transport union
            raise TypeError("MCP transport is open")
        mcp_servers.append(
            {
                "id": config.server_id,
                "name": config.display_name,
                "enabled": config.enabled,
                "status": status,
                "required": config.required,
                "available_to_subagents": (
                    config.scope_policy is McpScopePolicy.ROOT_AND_SUBAGENTS
                ),
                "tool_count": live.exposed_tool_count if live is not None else 0,
                "resource_count": live.resource_count if live is not None else 0,
                "resource_template_count": (
                    live.resource_template_count if live is not None else 0
                ),
                "prompt_count": live.prompt_count if live is not None else 0,
                "instructions": live.sanitized_instructions if live is not None else "",
                "has_failure": (
                    live is not None and live.stable_failure_category is not None
                ),
                "transport": transport,
                "tools": (
                    live_tools.get(config.server_id, [])
                    if live is not None
                    else []
                ),
            }
        )
    return {
        "roots": [
            {"kind": "agents", "path": str(Path.home() / ".agents")},
            {"kind": "pulsara", "path": str(require_pulsara_home())},
        ],
        "skills": skills,
        "mcp": {
            "config_path": str(DEFAULT_USER_MCP_CONFIG.expanduser()),
            "servers": mcp_servers,
        },
        "plugins": _user_plugins_payload(plugins),
    }


def _user_plugins_payload(inspection: object) -> dict[str, object]:
    if isinstance(inspection, PluginInspectionAbort):
        return {
            "status": "attention",
            "items": [],
            "details": ["插件清单暂时无法读取。"],
        }
    if not isinstance(inspection, PluginInspectionOutcome):
        raise TypeError("Plugin inspection result is open")
    if inspection.disposition is not PluginInspectionDisposition.COMPLETE:
        return {
            "status": "attention",
            "items": [],
            "details": [_diagnostic_message(item) for item in inspection.diagnostics],
        }
    items = []
    for item in inspection.instances:
        if item.identity.scope is not PluginScopeKind.USER:
            continue
        manifest = item.summary.manifest
        items.append(
            {
                "id": item.identity.plugin_id,
                "name": manifest.name,
                "description": manifest.description or "这个插件没有提供说明。",
                "version": manifest.version,
                "author": manifest.author.name if manifest.author is not None else None,
                "enabled": item.enabled,
                "package_install_id": item.package_install_id,
                "package_root": str(item.package_root),
                "skill_count": len(item.summary.skills.skills),
                "mcp_count": len(item.summary.mcp.mcp_servers),
                "effective_skill_names": list(item.effective_skill_names),
                "effective_mcp_server_ids": list(item.effective_mcp_server_ids),
                "details": [_diagnostic_message(value) for value in item.diagnostics],
            }
        )
    return {
        "status": "ready",
        "items": sorted(items, key=lambda item: str(item["name"])),
        "details": [_diagnostic_message(item) for item in inspection.diagnostics],
    }


def _diagnostic_message(value: object) -> str:
    message = getattr(value, "message", None)
    return str(message) if isinstance(message, str) and message else type(value).__name__


def _plugin_install_operation(outcome: object) -> dict[str, object]:
    if isinstance(outcome, CleanupUnavailablePluginInstallOutcome):
        result = _plugin_install_operation(outcome.prior)
        result["status"] = outcome.disposition.value
        result["message"] = f"{result['message']} 旧安装文件稍后可以清理。"
        return result
    if isinstance(outcome, (SuccessfulPluginInstallOutcome, AlreadyPresentPluginInstallOutcome)):
        installed = isinstance(outcome, SuccessfulPluginInstallOutcome)
        return {
            "status": outcome.disposition.value,
            "success": True,
            "message": (
                "插件已经安装；开启后会用于当前打开的会话。"
                if installed
                else "这个插件已经安装。"
            ),
            "plugin_id": outcome.identity.plugin_id,
        }
    if isinstance(outcome, FailedPluginInstallOutcome):
        return {
            "status": outcome.disposition.value,
            "success": False,
            "message": "插件没有安装，请检查所选目录。",
            "details": [_diagnostic_message(item) for item in outcome.diagnostics],
        }
    raise TypeError("Plugin install outcome is open")


def _plugin_enablement_operation(outcome: object) -> dict[str, object]:
    if isinstance(outcome, SettledPluginEnablementOutcome):
        return {
            "status": outcome.disposition.value,
            "success": True,
            "message": "插件已开启。" if outcome.enabled else "插件已关闭。",
        }
    if isinstance(outcome, FailedPluginEnablementOutcome):
        return {
            "status": outcome.disposition.value,
            "success": False,
            "message": "插件状态没有改变，请刷新后重试。",
            "details": [_diagnostic_message(item) for item in outcome.diagnostics],
        }
    raise TypeError("Plugin enablement outcome is open")


def _plugin_removal_operation(outcome: object) -> dict[str, object]:
    if isinstance(outcome, RemovedPluginOutcome):
        return {
            "status": outcome.disposition.value,
            "success": True,
            "message": "插件已移除。",
        }
    if isinstance(outcome, FailedPluginRemovalOutcome):
        return {
            "status": outcome.disposition.value,
            "success": False,
            "message": "插件没有移除，请刷新后重试。",
            "details": [_diagnostic_message(item) for item in outcome.diagnostics],
        }
    raise TypeError("Plugin removal outcome is open")


def _capability_payload(
    *,
    session_id: str,
    workspace_root: Path,
    inspection: KernelCapabilityCatalogInspection,
) -> dict[str, object]:
    tools_by_server: dict[str, list[dict[str, object]]] = {}
    for item in inspection.mcp_tools:
        tools_by_server.setdefault(item.server_id, []).append(
            {
                "name": item.provider_name,
                "remote_name": item.remote_name,
                "description": item.description,
                "effect": item.effect,
                "available_to_subagents": item.available_to_subagents,
                "parallel_safe": item.parallel_safe,
            }
        )
    catalog = inspection.mcp_catalog
    mcp_servers = [
        {
            "id": item.server_id,
            "name": item.display_name,
            "status": item.status.value,
            "required": item.required,
            "available_to_subagents": item.scope_subagents,
            "tool_count": item.exposed_tool_count,
            "discovered_tool_count": item.discovered_tool_count,
            "resource_count": item.resource_count,
            "resource_template_count": item.resource_template_count,
            "prompt_count": item.prompt_count,
            "instructions": item.sanitized_instructions,
            "has_failure": item.stable_failure_category is not None,
            "tools": tools_by_server.get(item.server_id, []),
        }
        for item in catalog.servers
    ]

    skill_catalog = inspection.skill_catalog
    skill_items: list[dict[str, object]] = []
    skill_issues: list[dict[str, object]] = []
    unavailable_details: list[str] = []
    if isinstance(skill_catalog, CompleteEffectiveSkillCatalogInspection):
        skill_items = [
            {
                "name": item.name,
                "description": item.description,
                "location": item.location,
                "source": item.source.value,
                "configured": item.name in inspection.configured_active_skill_names,
                "authoring_notes": [
                    code.value for code in item.authoring_diagnostic_codes
                ],
            }
            for item in skill_catalog.winners
        ]
        skill_issues = [
            _skill_issue_payload(item) for item in skill_catalog.candidate_issues
        ]
    elif isinstance(skill_catalog, UnavailableEffectiveSkillCatalogInspection):
        for cause in skill_catalog.unavailable_causes:
            if isinstance(cause, ProducerUnavailableCause):
                unavailable_details.extend(item.message for item in cause.diagnostics)
            elif isinstance(cause, ResolutionUnavailableCause):
                unavailable_details.append(cause.diagnostic.message)
            else:  # pragma: no cover - closed cause union
                raise TypeError("Skill catalog unavailable cause is open")
    else:  # pragma: no cover - closed inspection union
        raise TypeError("Skill catalog inspection is open")

    return {
        "session_id": session_id,
        "workspace_path": str(workspace_root),
        "skills": {
            "status": (
                "ready"
                if isinstance(skill_catalog, CompleteEffectiveSkillCatalogInspection)
                else "attention"
            ),
            "items": skill_items,
            "issues": skill_issues,
            "details": unavailable_details,
            "roots": [
                {
                    "path": str(item.path),
                    "scope": (
                        "workspace"
                        if item.root_kind.value.startswith("WORKSPACE_")
                        else "user"
                    ),
                }
                for item in skill_catalog.root_policy.roots
            ],
        },
        "mcp": {
            "servers": mcp_servers,
            "collisions": [
                {
                    "name": item.provider_name,
                    "members": [
                        {
                            "server_id": member.server_id,
                            "tool_name": member.remote_tool_name,
                        }
                        for member in item.members
                    ],
                }
                for item in catalog.provider_name_collision_facts
            ],
        },
    }


def _skill_issue_payload(issue: object) -> dict[str, object]:
    if isinstance(issue, InvalidSkillCandidateIssue):
        return {
            "kind": "invalid",
            "title": "有一个技能没有通过检查",
            "path": str(issue.path),
            "details": [item.message for item in issue.diagnostics],
        }
    if isinstance(issue, ShadowedSkillCandidateIssue):
        return {
            "kind": "shadowed",
            "title": f"{issue.name} 使用了优先级更高的版本",
            "path": str(issue.path),
            "details": [f"当前使用：{issue.winner_path}"],
        }
    if isinstance(issue, ConflictingSkillCandidateIssue):
        return {
            "kind": "conflict",
            "title": f"{issue.name} 存在同级重名",
            "details": [str(item.path) for item in issue.candidates],
        }
    raise TypeError("Skill candidate issue is open")


def _skill_install_payload(outcome: LocalSkillInstallOutcome) -> dict[str, object]:
    messages = {
        "INSTALLED": "技能已经安装。",
        "SOURCE_INVALID": "这个目录不是有效的技能。",
        "SOURCE_UNAVAILABLE": "无法读取这个技能目录。",
        "SOURCE_RACED": "安装时目录发生了变化，请重试。",
        "TARGET_CONFIGURATION_UNAVAILABLE": "无法准备技能安装位置。",
        "UNSUPPORTED_ENTRY": "技能目录包含不支持的内容。",
        "DESTINATION_EXISTS": "同名技能已经存在。",
        "STAGING_UNAVAILABLE": "暂时无法准备安装文件。",
        "PUBLISH_UNAVAILABLE": "暂时无法完成安装。",
        "CANCELLED": "安装已取消。",
        "CLEANUP_UNAVAILABLE": "安装没有完成，临时文件需要留意。",
    }
    return {
        "status": outcome.disposition.value,
        "installed": outcome.disposition.value == "INSTALLED",
        "message": messages[outcome.disposition.value],
        "source_path": str(outcome.source_path),
        "destination_path": (
            str(outcome.destination_path)
            if outcome.destination_path is not None
            else None
        ),
        "details": [item.message for item in outcome.diagnostics],
    }


def _task_payload(
    task: Mapping[str, object],
    dependencies: list[dict[str, object]],
) -> dict[str, object]:
    accepted_at = task["accepted_at"]
    terminal_at = task.get("terminal_at")
    if not isinstance(accepted_at, datetime):
        raise RuntimeError("task row lacks its accepted timestamp")
    result_id = task.get("result_id")
    result = None
    if result_id is not None:
        result = {
            "id": str(result_id),
            "entry_id": task.get("result_entry_id"),
            "source": task.get("result_source"),
            "summary": task.get("result_summary"),
            "output_preview": task.get("result_output_preview"),
            "diagnostics": task.get("result_diagnostics") or [],
            "accepted": task.get("accepted_root_entry_id") is not None,
        }
    return {
        "id": str(task["id"]),
        "parent_turn_id": str(task["parent_turn_id"]),
        "batch_id": task.get("batch_id"),
        "task_key": task.get("task_key"),
        "label": task.get("label"),
        "profile": str(task["profile_kind"]),
        "display_role": task.get("display_role"),
        "context": {
            "mode": str(task["context_mode"]),
            "last_n_turns": task.get("context_last_n_turns"),
        },
        "objective": str(task["objective"]),
        "status": str(task["status"]),
        "pending_reason": task.get("pending_reason"),
        "terminal_reason": task.get("terminal_reason"),
        "terminal_public_detail": task.get("terminal_public_detail"),
        "completion_delivered": task.get("accepted_root_entry_id") is not None,
        "accepted_at": accepted_at.isoformat(),
        "terminal_at": (
            terminal_at.isoformat() if isinstance(terminal_at, datetime) else None
        ),
        "dependencies": dependencies,
        "result": result,
    }


def _encode_task_cursor(
    *,
    session_id: str,
    accepted_at: datetime,
    task_id: str,
    seen_count: int,
) -> str:
    body = json.dumps(
        {
            "v": 1,
            "session_id": session_id,
            "accepted_at": accepted_at.isoformat(),
            "task_id": task_id,
            "seen_count": seen_count,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii")


def _decode_task_cursor(
    cursor: str,
    *,
    session_id: str,
) -> tuple[datetime, str, int]:
    if not cursor or len(cursor.encode("utf-8")) > _TASK_CURSOR_MAXIMUM_BYTES:
        raise ValueError("task cursor is invalid")
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("task cursor is invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "v",
        "session_id",
        "accepted_at",
        "task_id",
        "seen_count",
    }:
        raise ValueError("task cursor is invalid")
    if value["v"] != 1 or value["session_id"] != session_id:
        raise ValueError("task cursor is invalid")
    task_id = value["task_id"]
    seen_count = value["seen_count"]
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task cursor is invalid")
    if (
        not isinstance(seen_count, int)
        or isinstance(seen_count, bool)
        or seen_count < 0
    ):
        raise ValueError("task cursor is invalid")
    try:
        accepted_at = datetime.fromisoformat(str(value["accepted_at"]))
    except ValueError as exc:
        raise ValueError("task cursor is invalid") from exc
    if accepted_at.tzinfo is None:
        raise ValueError("task cursor is invalid")
    return accepted_at, task_id, seen_count


__all__ = [
    "HostSessionHandle",
    "LocalSessionController",
    "SessionWorkspaceKind",
]
