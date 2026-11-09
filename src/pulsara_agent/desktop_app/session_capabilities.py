"""Desktop capability inspection and mutations, independent of session lifecycle."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from pulsara_agent.capability.local_skill_management import (
    InstallLooseLocalSkillRequest,
    LocalSkillInstallScope,
    LocalSkillManagementService,
    ValidateLocalSkillSourceRequest,
)
from pulsara_agent.capability.local_skill_removal import (
    LocalSkillRemovalDisposition,
    LocalSkillRemovalIdentity,
)
from pulsara_agent.capability.mcp_import import materialize_mcp_import, read_mcp_import
from pulsara_agent.capability.mcp_management import (
    LocalMcpTarget,
    McpSecretMutation,
    config_to_entry,
)
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.capability.skill_import import enumerate_skill_import_sources
from pulsara_agent.capability.user_skill_config import (
    set_user_skill_enabled as write_user_skill_enabled,
)
from pulsara_agent.capability.user_skill_config import workspace_skill_config_path
from pulsara_agent.desktop_app.capability_projection import (
    _plugin_enablement_operation,
    _plugin_install_operation,
    _plugin_removal_operation,
    _public_plugin_operation,
    _public_user_capabilities,
    _skill_install_payload,
    _user_capability_payload,
    _workspace_adoption_payload,
)
from pulsara_agent.desktop_app.skill_inventory import (
    _absolute_local_source,
    _inspect_user_skills,
    _inspect_workspace_skills,
    _workspace_skill_path,
)
from pulsara_agent.plugins.contracts import (
    CleanupUnavailablePluginInstallOutcome,
    PluginInspectionOutcome,
    PluginScopeKind,
    RemovedPluginOutcome,
    SettledPluginEnablementOutcome,
    SuccessfulPluginInstallOutcome,
)
from pulsara_agent.plugins.public_projection import project_hook_public
from pulsara_agent.process_credential_boundary import ProcessCredentialScrubSet
from pulsara_agent.public_projection import public_record


def _settled_capability_mutation(*, owns_lane):
    def decorate(operation):
        @wraps(operation)
        async def settled(self, *args, **kwargs):
            if owns_lane:
                # Preserve queued cancellation for GUI-owned filesystem writes.
                async with self._capability_mutation_lock:
                    return await _settle_capability_io(operation(self, *args, **kwargs))
            # MCP and Plugin connection/removal owners acquire the same lane
            # themselves. Never acquire it twice; join their result and refresh.
            return await _settle_capability_io(operation(self, *args, **kwargs))
        return settled
    return decorate


class SessionCapabilities:
    """Capability operations using the controller's existing core, locks, and hosts.

    This stateless mixin adds no lifetime or authority owner. Filesystem writes
    settle under the same mutation lane before adoption notifications and reads.
    """

    async def inspect_session_capabilities(self, session_id: str) -> dict[str, object]:
        """Read actual live observations; inspection never activates a Host."""
        summary = await self.core.read_resumable_session(session_id, memory_domain_id=self.memory_domain_id)
        if summary is None:
            raise KeyError(session_id)
        async with self._lock:
            handle = self._by_session.get(session_id)
        if handle is None:
            return {"available": False, "skills": {"items": [], "issues": [], "details": []},
                    "mcp": {"servers": []}, "plugins": {"items": []}}
        return await self._session_capability_payload(handle)

    async def reconnect_session_mcp(
        self, session_id: str, server_id: str
    ) -> dict[str, object]:
        if not server_id:
            raise ValueError("MCP server id is required")
        handle = await self.resume_session(session_id)
        handle.session.reconnect_mcp_server(server_id)
        return await self._session_capability_payload(handle)

    @_settled_capability_mutation(owns_lane=True)
    async def install_session_skill(
        self,
        session_id: str,
        *,
        source_path: str,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, object]:
        source = Path(source_path.strip()).expanduser()
        if not source_path.strip() or not source.is_absolute():
            raise ValueError("Skill source path must be absolute")
        handle = await self.resume_session(session_id)
        workspace_root = handle.session.workspace.workspace_root
        outcome = await _settle_capability_io(
            asyncio.to_thread(
                LocalSkillManagementService().install_loose_local_skill,
                InstallLooseLocalSkillRequest(
                    source_path=source,
                    scope=LocalSkillInstallScope.WORKSPACE,
                    workspace_root=workspace_root,
                    name=name,
                    description=description,
                ),
            )
        )
        pending = (
            await self._mark_capability_refresh(workspace_root)
            if outcome.destination_path is not None and (outcome.disposition.value == "INSTALLED" or (outcome.prior_disposition is not None and outcome.prior_disposition.value == "INSTALLED"))
            else 0
        )
        capabilities = await self._session_capability_payload(handle)
        return {
            "installation": _skill_install_payload(outcome),
            "adoption": _workspace_adoption_payload(pending),
            "capabilities": capabilities,
        }

    @_settled_capability_mutation(owns_lane=True)
    async def set_session_skill_enabled(
        self,
        session_id: str,
        *,
        skill_id: str,
        enabled: bool,
    ) -> dict[str, object]:
        handle = await self.resume_session(session_id)
        workspace_root = handle.session.workspace.workspace_root
        observed = await asyncio.to_thread(
            _inspect_workspace_skills, workspace_root
        )
        skill_path = _workspace_skill_path(observed, skill_id)
        await asyncio.to_thread(
            write_user_skill_enabled,
            skill_path=skill_path,
            enabled=enabled,
            config_path=workspace_skill_config_path(workspace_root),
        )
        pending = await self._mark_capability_refresh(workspace_root)
        capabilities = await self._session_capability_payload(handle)
        return {
            "operation": {
                "status": "ENABLED" if enabled else "DISABLED",
                "success": True,
                "message": (
                    "项目技能已开启，将在会话的安全时机采用。"
                    if enabled
                    else "项目技能已关闭，将在会话的安全时机停用。"
                ),
            },
            "adoption": _workspace_adoption_payload(pending),
            "capabilities": capabilities,
        }

    @_settled_capability_mutation(owns_lane=True)
    async def remove_session_skill(self, session_id, *, skill_path, expected):
        handle = await self.resume_session(session_id)
        workspace_root = handle.session.workspace.workspace_root
        outcome = await _settle_capability_io(
            asyncio.to_thread(
                LocalSkillManagementService().remove_loose_local_skill,
                skill_path=Path(skill_path),
                scope=LocalSkillInstallScope.WORKSPACE,
                expected=expected,
                workspace_root=workspace_root,
            )
        )
        changed = outcome.disposition in {
            LocalSkillRemovalDisposition.REMOVED,
            LocalSkillRemovalDisposition.CLEANUP_ATTENTION,
        }
        pending = await self._mark_capability_refresh(workspace_root) if changed else 0
        return {
            "operation": {
                "status": outcome.disposition.value,
                "success": changed,
                "message": "项目技能安装副本已删除。"
                if changed
                else "技能未删除，请刷新后重新确认。",
                "cleanup_attention": outcome.disposition
                is LocalSkillRemovalDisposition.CLEANUP_ATTENTION,
            },
            "adoption": _workspace_adoption_payload(pending),
            "capabilities": await self._session_capability_payload(handle),
        }

    @_settled_capability_mutation(owns_lane=False)
    async def create_session_mcp_server(
        self,
        session_id: str,
        *,
        server_id: str,
        config: dict[str, object],
        secret_changes: tuple[McpSecretMutation, ...] = (),
    ) -> dict[str, object]:
        handle = await self.resume_session(session_id)
        workspace_root = handle.session.workspace.workspace_root
        managed_server_ids = tuple(
            item.server_id
            for item in handle.session.inspect_capability_catalog().mcp_configured_servers
            if item.source_kind == "MANAGED_PACKAGE"
        )
        outcome = await self.core.mcp_management.create(
            LocalMcpTarget(server_id.strip(), workspace_root),
            config,
            secrets=secret_changes,
            managed_server_ids=managed_server_ids,
        )
        return await self._workspace_mcp_settlement(handle, outcome, "ADDED", server_id=server_id.strip())

    @_settled_capability_mutation(owns_lane=False)
    async def update_session_mcp_server(
        self,
        session_id,
        *,
        server_id,
        config,
        expected_identity,
        secret_changes=(),
        retain_credentials_confirmed=False,
    ):
        handle = await self.resume_session(session_id)
        result = await self.core.mcp_management.update(
            LocalMcpTarget(server_id, handle.session.workspace.workspace_root),
            config,
            expected=expected_identity,
            secrets=secret_changes,
            retain_credentials_confirmed=retain_credentials_confirmed,
        )
        return await self._workspace_mcp_settlement(handle, result, "UPDATED", server_id=server_id)

    @_settled_capability_mutation(owns_lane=False)
    async def set_session_mcp_enabled(
        self,
        session_id: str,
        *,
        server_id: str,
        enabled: bool,
        expected_config_identity: str,
    ) -> dict[str, object]:
        handle = await self.resume_session(session_id)
        target = LocalMcpTarget(server_id, handle.session.workspace.workspace_root)
        current = self.core.mcp_management.inspect(target)
        if current is None:
            raise KeyError(server_id)
        outcome = await self.core.mcp_management.update(
            target,
            {**config_to_entry(current), "enabled": enabled},
            expected=expected_config_identity,
        )
        return await self._workspace_mcp_settlement(
            handle, outcome, "ENABLED" if enabled else "DISABLED", server_id=server_id
        )

    @_settled_capability_mutation(owns_lane=False)
    async def remove_session_mcp_server(
        self,
        session_id: str,
        *,
        server_id: str,
        expected_config_identity: str,
    ) -> dict[str, object]:
        handle = await self.resume_session(session_id)
        target = LocalMcpTarget(server_id, handle.session.workspace.workspace_root)
        outcome = await self.core.mcp_management.remove(
            target, expected=expected_config_identity
        )
        return await self._workspace_mcp_settlement(handle, outcome, "REMOVED", server_id=server_id)

    async def _workspace_mcp_settlement(self, handle, outcome, status, *, server_id):
        pending = await self._mark_capability_refresh(handle.session.workspace.workspace_root) if outcome.applied else 0
        return {
            "operation": {
                "status": status,
                "success": outcome.applied,
                "message": "项目 MCP 已更新，后续调用将使用新配置。",
                "cleanup_attention": outcome.cleanup_attention,
            },
            "adoption": _workspace_adoption_payload(pending),
            "capabilities": await self._session_capability_payload(handle),
        }

    async def inspect_user_capabilities(self) -> dict[str, object]:
        """Read the user-owned Skill, MCP, and Plugin inventory."""

        skills_task = asyncio.to_thread(_inspect_user_skills)
        mcp_task = asyncio.to_thread(self.core.mcp_management.load_configs)
        plugins_task = self.core.inspect_user_plugins()
        skills, mcp_configs, plugins = await asyncio.gather(
            skills_task, mcp_task, plugins_task
        )
        try:
            # Read only published, available owners. Lifecycle operations can
            # already be quiescing a Host that is still in the publication map.
            # This read never resumes a cold Session or starts another MCP.
            async with self._lock:
                live_inspections = tuple(
                    handle.session.inspect_capability_catalog()
                    for session_id, handle in sorted(self._by_session.items())
                    if session_id not in self._operations
                )
            payload = _user_capability_payload(
                skills=skills,
                mcp_configs=mcp_configs,
                plugins=plugins,
                secret_resolver=self.core.mcp_management.settings.read().mcp_secret,
                live_inspections=live_inspections,
            )
            scrub = plugins.scrub_set if isinstance(plugins, PluginInspectionOutcome) else None
            if scrub is None:
                scrub = ProcessCredentialScrubSet()
            return _public_user_capabilities(payload, scrub)
        finally:
            if isinstance(plugins, PluginInspectionOutcome):
                plugins.close()

    def _user_hook_provider(self):
        from pulsara_agent.hooks.source import LocalHookSourceProvider

        return LocalHookSourceProvider(
            workspace_root=None, workspace_kind=None, workspace_state_key=None
        )

    @contextmanager
    def _user_hook_sources(self, deadline, *, local_only=False):
        from pulsara_agent.hooks.contracts import FrozenHookDefinitionView
        from pulsara_agent.plugins.contracts import (
            EnabledPluginViewDisposition,
            NeverCancelPluginOperation,
        )
        from pulsara_agent.plugins.view import EnabledPluginViewOwner
        from pulsara_agent.plugins.package_core import (
            PluginPackageCancelled,
            PluginPackageTimedOut,
        )
        from pulsara_agent.plugins.hook_adapter import compose_hook_definition_view

        provider = self._user_hook_provider()
        local = provider.discover(deadline_monotonic=deadline)
        if local_only:
            yield local.source_snapshots, []
            return
        plugins = self.core._plugin_management()
        store = plugins._store()
        if store is None:
            yield local.source_snapshots, ["插件 Hook 来源当前不可读取。"]
            return
        try:
            view = EnabledPluginViewOwner(
                store=store, credential_boundary=self.core._credential_boundary
            ).observe(
                workspace_root=None,
                deadline_monotonic=deadline,
                cancellation=NeverCancelPluginOperation(),
            )
        except (PluginPackageCancelled, PluginPackageTimedOut, TimeoutError):
            yield local.source_snapshots, ["插件 Hook 来源当前不可读取。"]
            return
        composed = FrozenHookDefinitionView(())
        try:
            composed = compose_hook_definition_view(
                local_view=local, plugin_view=view, trust_store=provider.trust_store
            )
            details = (
                ["插件 Hook 来源当前不可读取。"]
                if view.disposition is EnabledPluginViewDisposition.UNAVAILABLE
                else []
            )
            yield composed.source_snapshots, details
        finally:
            # Hooks own duplicate anchors in addition to the instance anchor.
            anchors = [
                s.provenance.physical_lifetime_anchor for s in composed.source_snapshots
            ]
            anchors.extend(
                i.hooks.parsed.provenance.physical_lifetime_anchor
                for i in view.instances
                if i.hooks.parsed is not None
            )
            for anchor in {id(a): a for a in anchors if a is not None}.values():
                anchor.close()
            view.close()

    def _hook_public(self, snapshot, *, inspect):
        from pulsara_agent.hooks.presentation import hook_snapshot_public

        value = hook_snapshot_public(snapshot, inspect=inspect)
        current, trusted = (
            snapshot.trust.current_definition_digest,
            snapshot.trust.trusted_definition_digest,
        )
        value["trust_state"] = (
            "UNAVAILABLE"
            if current is None or snapshot.trust.disposition.value == "UNAVAILABLE"
            else "UNTRUSTED"
            if trusted is None
            else "TRUSTED"
            if current == trusted
            else "MODIFIED"
        )
        value["definition_count"] = len(snapshot.definitions)
        value["can_trust"] = (
            snapshot.disposition.value == "COMPLETE"
            and snapshot.trust.disposition.value != "UNAVAILABLE"
            and bool(snapshot.definitions)
        )
        return value

    @staticmethod
    def _select_user_hook(snapshots, plugin_id):
        from pulsara_agent.hooks.contracts import PluginHookSourceIdentity

        for snapshot in snapshots:
            identity = snapshot.provenance.identity
            if isinstance(identity, PluginHookSourceIdentity):
                if plugin_id == identity.plugin_id:
                    return snapshot
            elif plugin_id is None:
                return snapshot
        from pulsara_agent.hooks.source import HookSourceConflict

        raise HookSourceConflict("Hook 来源已关闭、替换或不可读取，请重新审阅。")

    async def read_user_hooks(self):
        deadline = self.core._canonical_deadline()

        def read():
            # Freeze these independent local observations before plugin I/O can
            # consume the operation deadline; a failed plugin view is partial UI.
            scrub = self.core._plugin_management()._capture_scrub_set(
                _hook_inspection_request(deadline)
            )
            try:
                local_info = {
                    "exists": self._user_hook_provider()
                    .read_user_config(deadline_monotonic=deadline)
                    .exists
                }
            except (OSError, ValueError):
                local_info = {"editable": False}
            with self._user_hook_sources(deadline) as (sources, details):
                payload = {
                    "sources": [self._hook_public(s, inspect=True) for s in sources],
                    "details": details,
                }
                payload["sources"][0].update(local_info)
                return public_record(payload, scrub, children={
                    "sources": [project_hook_public(source, scrub) for source in payload["sources"]],
                })

        return await asyncio.to_thread(read)

    async def read_user_hook_review(self, plugin_id=None):
        deadline = self.core._canonical_deadline()

        def read():
            scrub = self.core._plugin_management()._capture_scrub_set(
                _hook_inspection_request(deadline)
            )
            with self._user_hook_sources(deadline, local_only=plugin_id is None) as (
                sources,
                _,
            ):
                snapshot = self._select_user_hook(sources, plugin_id)
                value = self._hook_public(snapshot, inspect=True)
                if not value["can_trust"]:
                    from pulsara_agent.hooks.source import HookSourceConflict

                    raise HookSourceConflict(
                        "需要可读取且非空的完整 Hook 定义才能审阅批准。"
                    )
                return project_hook_public(value, scrub)

        return await asyncio.to_thread(read)

    async def read_user_hook_config(self):
        deadline = self.core._canonical_deadline()
        provider = self._user_hook_provider()
        observation = await asyncio.to_thread(
            provider.read_user_config, deadline_monotonic=deadline
        )
        return {
            "exists": observation.exists,
            "text": observation.text,
            "path": str(provider.user_config_path),
        }

    async def hook_error_message(self, error):
        scrub = await asyncio.to_thread(
            self.core._plugin_management()._capture_scrub_set,
            _hook_inspection_request(self.core._canonical_deadline()),
        )
        return scrub.scrub_text(str(error))

    async def _mutate_user_hook(self, operation):
        from pulsara_agent.hooks.trust import HookMutationPublishedError

        async def settle():
            message = None
            try:
                await asyncio.to_thread(operation)
            except HookMutationPublishedError as exc:
                message = str(exc)
            try:
                pending = await self._mark_capability_refresh()
            except Exception:
                pending = 0
                message = message or "已写入，但会话刷新通知未完成，请刷新确认。"
            return {
                "status": "ATTENTION" if message else "APPLIED",
                "message": message,
                "pending_sessions": pending,
            }

        async with self._capability_mutation_lock:
            return await _settle_capability_io(settle())

    async def save_user_hook_config(self, *, exists, expected_text, text):
        from pulsara_agent.hooks.source import LocalHookConfigText

        provider = self._user_hook_provider()
        deadline = self.core._canonical_deadline()
        return await self._mutate_user_hook(
            lambda: provider.save_user_config(
                expected=LocalHookConfigText(exists, expected_text),
                text=text,
                deadline_monotonic=deadline,
            ),
        )

    async def trust_user_hook(self, *, plugin_id, definition_digest, trust, accepted):
        from pulsara_agent.hooks.contracts import (
            HookSourceKind,
            HookVisibilityScope,
            PluginHookTrustSubject,
        )
        from pulsara_agent.hooks.source import HookSourceConflict

        if accepted is not True:
            raise ValueError("请先审阅并勾选信任这些 Hook 定义。")
        provider = self._user_hook_provider()
        subject = (
            provider.source_subject(HookSourceKind.USER_FILE)
            if plugin_id is None
            else PluginHookTrustSubject(HookVisibilityScope.USER, plugin_id)
        )
        deadline = self.core._canonical_deadline()

        def current():
            with self._user_hook_sources(deadline, local_only=plugin_id is None) as (
                sources,
                _,
            ):
                snapshot = self._select_user_hook(sources, plugin_id)
                value = self._hook_public(snapshot, inspect=False)
                actual = {
                    k: value[k]
                    for k in ("enabled", "trusted_definition_digest", "trusted_at")
                }
                if (
                    not value["can_trust"]
                    or actual != trust
                    or value["definition_digest"] != definition_digest
                ):
                    raise HookSourceConflict("Hook 定义或信任状态已改变，请重新审阅。")
                return value["definition_digest"]

        return await self._mutate_user_hook(
            lambda: provider.trust_store.trust_after_revalidation(
                subject,
                expected_digest=definition_digest,
                current_digest_reader=current,
                deadline_monotonic=deadline,
            ),
        )

    async def refresh_user_capabilities(self) -> dict[str, object]:
        """Adopt current user sources in every live Session, then inspect them."""

        updated, attention = await self._reload_live_capabilities()
        payload = await self.inspect_user_capabilities()
        payload["adoption"] = {
            "updated_sessions": updated,
            "attention_sessions": attention,
        }
        return payload

    async def preview_skill_import(self, *, source_path: str) -> dict[str, object]:
        source = _absolute_local_source(source_path, "Skill")

        def inspect():
            service = LocalSkillManagementService()
            items = []
            for path in enumerate_skill_import_sources(source):
                result = service.validate_local_skill_source(
                    ValidateLocalSkillSourceRequest(path)
                )
                items.append(
                    {
                        "source_path": str(path),
                        "name": result.parsed.name if result.parsed else path.name,
                        "description": result.parsed.description
                        if result.parsed
                        else "",
                        "valid": result.parsed is not None,
                        "details": [item.message for item in result.diagnostics]
                        + (
                            ["暂时无法读取这个技能目录。"]
                            if result.unavailable_reason
                            else []
                        ),
                    }
                )
            return {"items": items}

        return await asyncio.to_thread(inspect)

    @_settled_capability_mutation(owns_lane=True)
    async def install_user_skill(
        self,
        *,
        source_path: str,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, object]:
        source = _absolute_local_source(source_path, "Skill")
        outcome = await _settle_capability_io(
            asyncio.to_thread(
                LocalSkillManagementService().install_loose_local_skill,
                InstallLooseLocalSkillRequest(
                    source_path=source,
                    scope=LocalSkillInstallScope.USER,
                    name=name,
                    description=description,
                ),
            )
        )
        pending = (
            await self._mark_capability_refresh()
            if outcome.destination_path is not None and (outcome.disposition.value == "INSTALLED" or (outcome.prior_disposition is not None and outcome.prior_disposition.value == "INSTALLED"))
            else 0
        )
        capabilities = await self.inspect_user_capabilities()
        return {
            "operation": _skill_install_payload(outcome),
            "adoption": {
                "updated_sessions": 0,
                "pending_sessions": pending,
                "attention_sessions": 0,
            },
            "capabilities": capabilities,
        }

    @_settled_capability_mutation(owns_lane=True)
    async def set_user_skill_enabled(
        self,
        *,
        skill_path: str,
        enabled: bool,
    ) -> dict[str, object]:
        requested = Path(skill_path.strip()).expanduser()
        if not skill_path.strip() or not requested.is_absolute():
            raise ValueError("Skill path must be absolute")
        observed = await asyncio.to_thread(_inspect_user_skills)
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
        pending = await self._mark_capability_refresh()
        capabilities = await self.inspect_user_capabilities()
        return {
            "adoption": {
                "updated_sessions": 0,
                "pending_sessions": pending,
                "attention_sessions": 0,
            },
            "operation": {
                "status": "ENABLED" if enabled else "DISABLED",
                "success": True,
                "message": "技能已开启。" if enabled else "技能已关闭。",
            },
            "capabilities": capabilities,
        }

    @_settled_capability_mutation(owns_lane=True)
    async def remove_user_skill(
        self,
        *,
        skill_path: str,
        expected: LocalSkillRemovalIdentity,
    ) -> dict[str, object]:
        path = Path(skill_path).expanduser()
        service = LocalSkillManagementService()
        work = asyncio.create_task(
            asyncio.to_thread(
                service.remove_loose_local_skill,
                skill_path=path,
                scope=LocalSkillInstallScope.USER,
                expected=expected,
            )
        )
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        outcome = work.result()
        changed = outcome.disposition in {
            LocalSkillRemovalDisposition.REMOVED,
            LocalSkillRemovalDisposition.CLEANUP_ATTENTION,
        }
        pending = await self._mark_capability_refresh() if changed else 0
        return {
            "operation": {
                "status": outcome.disposition.value,
                "success": changed,
                "message": {
                    LocalSkillRemovalDisposition.REMOVED: "技能安装副本已删除，原始来源未改变。",
                    LocalSkillRemovalDisposition.NOT_FOUND: "该技能已经不存在。",
                    LocalSkillRemovalDisposition.STALE: "技能目录已改变，请刷新后重新确认。",
                    LocalSkillRemovalDisposition.CLEANUP_ATTENTION: "技能已从列表移除，但部分本地清理未完成。",
                }[outcome.disposition],
            },
            "adoption": {
                "updated_sessions": 0,
                "pending_sessions": pending,
                "attention_sessions": 0,
            },
            "capabilities": await self.inspect_user_capabilities(),
        }

    @_settled_capability_mutation(owns_lane=False)
    async def create_user_mcp_server(
        self,
        *,
        server_id: str,
        config: dict[str, object],
        secret_changes: tuple[McpSecretMutation, ...] = (),
    ) -> dict[str, object]:
        outcome = await self.core.mcp_management.create(
            LocalMcpTarget(server_id),
            config,
            secret_changes,
        )
        return await self._user_mcp_settlement(outcome, "ADDED", server_id=server_id)

    async def preview_mcp_import(
        self, *, content: str, shape: str, server_id: str | None
    ):
        drafts = await asyncio.to_thread(
            read_mcp_import, content, shape=shape, server_id=server_id
        )
        return {"items": [draft.preview() for draft in drafts]}

    @_settled_capability_mutation(owns_lane=False)
    async def import_mcp(
        self,
        *,
        content: str,
        shape: str,
        server_id: str | None,
        selected_server_id: str,
        classifications: dict[str, str],
        values: dict[str, str],
        transport: str | None,
        allow_http_localhost: bool = False,
        session_id: str | None = None,
    ):
        drafts = await asyncio.to_thread(
            read_mcp_import, content, shape=shape, server_id=server_id
        )
        selected = [draft for draft in drafts if draft.server_id == selected_server_id]
        if len(selected) != 1:
            raise ValueError("请选择一个明确的 MCP 服务。")
        handle = (
            await self.resume_session(session_id) if session_id is not None else None
        )
        target = LocalMcpTarget(
            selected_server_id,
            handle.session.workspace.workspace_root if handle else None,
        )
        entry, secrets = materialize_mcp_import(
            selected[0],
            target.owner,
            classifications=classifications,
            values=values,
            transport=transport,
            allow_http_localhost=allow_http_localhost,
        )
        managed_ids = (
            tuple(
                item.server_id
                for item in handle.session.inspect_capability_catalog().mcp_configured_servers
                if item.source_kind == "MANAGED_PACKAGE"
            )
            if handle
            else ()
        )
        outcome = await self.core.mcp_management.create(
            target, entry, secrets, managed_server_ids=managed_ids
        )
        if handle:
            return await self._workspace_mcp_settlement(handle, outcome, "ADDED", server_id=selected_server_id)
        return await self._user_mcp_settlement(outcome, "ADDED", server_id=selected_server_id)

    @_settled_capability_mutation(owns_lane=False)
    async def update_user_mcp_server(
        self,
        *,
        server_id: str,
        config: dict[str, object],
        expected: str,
        secret_changes: tuple[McpSecretMutation, ...] = (),
        retain_credentials_confirmed: bool = False,
    ) -> dict[str, object]:
        outcome = await self.core.mcp_management.update(
            LocalMcpTarget(server_id),
            config,
            expected=expected,
            secrets=secret_changes,
            retain_credentials_confirmed=retain_credentials_confirmed,
        )
        return await self._user_mcp_settlement(outcome, "UPDATED", server_id=server_id)

    async def test_mcp_server(
        self,
        *,
        server_id: str,
        config: dict[str, object],
        secret_changes: tuple[McpSecretMutation, ...] = (),
        retain_credentials_confirmed: bool = False,
        session_id: str | None = None,
    ) -> dict[str, object]:
        handle = (
            await self.resume_session(session_id) if session_id is not None else None
        )
        workspace_root = (
            handle.session.workspace.workspace_root
            if handle
            else require_pulsara_home()
        )
        result = await self.core.mcp_management.test(
            LocalMcpTarget(server_id, workspace_root if handle else None),
            config,
            workspace_root=workspace_root,
            secrets=secret_changes,
            retain_credentials_confirmed=retain_credentials_confirmed,
        )
        return {
            "status": result.status,
            "tools": result.tools,
            "resources": result.resources,
            "resource_templates": result.resource_templates,
            "prompts": result.prompts,
        }

    @_settled_capability_mutation(owns_lane=False)
    async def remove_user_mcp_server(
        self,
        *,
        server_id: str,
        expected: str,
    ) -> dict[str, object]:
        outcome = await self.core.mcp_management.remove(
            LocalMcpTarget(server_id), expected=expected
        )
        return await self._user_mcp_settlement(outcome, "REMOVED", server_id=server_id)

    async def _user_mcp_settlement(self, outcome, status, *, server_id):
        # No canonical mutation lane is held while physical consumers adopt/drain.
        pending = await self._mark_capability_refresh() if outcome.applied else 0
        return {
            "operation": {
                "status": status,
                "success": outcome.applied,
                "message": "MCP 配置已保存。"
                if status != "REMOVED"
                else "MCP 服务已移除。",
                "cleanup_attention": outcome.cleanup_attention,
            },
            "adoption": {
                "updated_sessions": 0,
                "pending_sessions": pending,
                "attention_sessions": 0,
            },
            "capabilities": await self.inspect_user_capabilities(),
        }

    async def authorize_mcp(
        self, server_id: str, *, session_id: str | None = None
    ) -> dict[str, object]:
        handle = (
            await self.resume_session(session_id) if session_id is not None else None
        )
        target = LocalMcpTarget(
            server_id, handle.session.workspace.workspace_root if handle else None
        )
        flow = await self.core.mcp_management.authorize(target)
        return {"state": flow.state, "error": flow.error}

    async def mcp_authorization(
        self,
        server_id: str,
        *,
        action: str,
        session_id: str | None = None,
    ) -> dict[str, object]:
        if action not in {"status", "cancel", "logout"}:
            raise ValueError("invalid MCP authorization action")
        workspace_root = None
        if session_id is not None:
            if action == "status":
                summary = await self.core.read_resumable_session(
                    session_id, memory_domain_id=self.memory_domain_id)
                if summary is None:
                    raise KeyError(session_id)
                workspace_root = Path(summary.workspace_root)
            else:
                handle = await self.resume_session(session_id)
                workspace_root = handle.session.workspace.workspace_root
        target = LocalMcpTarget(server_id, workspace_root)
        owner = target.owner
        manager = await self.core.mcp_management.prepare_oauth()
        if action == "cancel":
            await manager.cancel(owner)
        elif action == "logout":
            await manager.logout(owner)
        elif action != "status":
            raise ValueError("invalid MCP authorization action")
        config = await asyncio.to_thread(self.core.mcp_management.inspect, target)
        if config is None:
            return {"state": "idle", "error": None}
        return manager.login_state(owner, config)

    @_settled_capability_mutation(owns_lane=False)
    async def install_user_plugin(
        self,
        *,
        source_path: str,
        source_format="native",
        classifications=(),
        public_values=(),
        mcp_scope_policies=(),
    ) -> dict[str, object]:
        source = _absolute_local_source(source_path, "Plugin")
        outcome = await self.core.install_user_plugin(
            source,
            source_format=source_format,
            import_classifications=classifications,
            import_public_values=public_values,
            mcp_scope_policies=mcp_scope_policies,
        )
        published = outcome.prior if isinstance(outcome, CleanupUnavailablePluginInstallOutcome) else outcome
        if isinstance(published, SuccessfulPluginInstallOutcome):
            await self._mark_capability_refresh()
        capabilities = await self.inspect_user_capabilities()
        return {
            "operation": _public_plugin_operation(outcome, _plugin_install_operation(outcome)),
            "capabilities": capabilities,
        }

    async def preview_plugin_import(self, *, source_path):
        from pulsara_agent.plugins.source_import import preview_plugin_imports
        from pulsara_agent.plugins.contracts import NeverCancelPluginOperation

        source = _absolute_local_source(source_path, "Plugin")
        return await asyncio.to_thread(
            preview_plugin_imports,
            source,
            deadline_monotonic=self.core._canonical_deadline(),
            cancellation=NeverCancelPluginOperation(),
            credential_boundary=self.core._credential_boundary,
        )

    @_settled_capability_mutation(owns_lane=True)
    async def set_user_plugin_enabled(
        self,
        *,
        plugin_id: str,
        package_install_id: str,
        enabled: bool,
        connection_review,
    ) -> dict[str, object]:
        outcome = await self.core.set_user_plugin_enabled(
            plugin_id=plugin_id,
            package_install_id=package_install_id,
            enabled=enabled,
            connection_review=connection_review,
        )
        pending = (
            await self._mark_capability_refresh()
            if isinstance(outcome, SettledPluginEnablementOutcome)
            else 0
        )
        capabilities = await self.inspect_user_capabilities()
        return {
            "operation": _public_plugin_operation(outcome, _plugin_enablement_operation(outcome)),
            "adoption": {
                "updated_sessions": 0,
                "pending_sessions": pending,
                "attention_sessions": 0,
            },
            "capabilities": capabilities,
        }

    @_settled_capability_mutation(owns_lane=False)
    async def remove_user_plugin(
        self,
        *,
        plugin_id: str,
        package_install_id: str,
    ) -> dict[str, object]:
        outcome = await self.core.remove_user_plugin(plugin_id, package_install_id)
        pending = (
            await self._mark_capability_refresh()
            if isinstance(outcome, RemovedPluginOutcome)
            else 0
        )
        capabilities = await self.inspect_user_capabilities()
        return {
            "operation": _public_plugin_operation(outcome, _plugin_removal_operation(outcome)),
            "adoption": {
                "updated_sessions": 0,
                "pending_sessions": pending,
                "attention_sessions": 0,
            },
            "capabilities": capabilities,
        }

    @_settled_capability_mutation(owns_lane=False)
    async def replace_user_plugin_connection(
        self,
        *,
        plugin_id,
        server_id,
        package_install_id,
        expected_overlay,
        overlay,
        secret_changes,
        retain_credentials_confirmed=False,
    ):
        from pulsara_agent.plugins.connection_management import (
            ReplacePluginMcpConnectionRequest,
        )
        from pulsara_agent.plugins.contracts import PluginInstanceIdentity

        result = (
            await self.core._plugin_management().replace_plugin_mcp_connection_overlay(
                ReplacePluginMcpConnectionRequest(
                    PluginInstanceIdentity(PluginScopeKind.USER, plugin_id),
                    server_id,
                    package_install_id,
                    expected_overlay,
                    overlay,
                    self.core._canonical_deadline(),
                    secret_changes,
                    retain_credentials_confirmed,
                ),
                connections=self.core.mcp_management,
            )
        )
        pending = await self._mark_capability_refresh() if result.applied else 0
        return {
            "operation": {
                "status": "UPDATED",
                "success": result.applied,
                "cleanup_attention": result.cleanup_attention,
                "message": "插件连接已保存。",
            },
            "adoption": {
                "updated_sessions": 0,
                "pending_sessions": pending,
                "attention_sessions": 0,
            },
            "capabilities": await self.inspect_user_capabilities(),
        }

    async def plugin_mcp_authorization(
        self, *, plugin_id, server_id, package_install_id, action
    ):
        from pulsara_agent.plugins.contracts import PluginInstanceIdentity

        result = await self.core._plugin_management().authorize_plugin_mcp(
            identity=PluginInstanceIdentity(PluginScopeKind.USER, plugin_id),
            local_server_id=server_id,
            expected_package_install_id=package_install_id,
            deadline_monotonic=self.core._canonical_deadline(),
            connections=self.core.mcp_management,
            action=action,
        )
        return (
            {"state": result.state, "error": result.error}
            if action == "login"
            else result
        )

    async def open_capability_root(self, root: str) -> dict[str, object]:
        if root == "agents":
            path = Path.home() / ".agents"
        elif root == "pulsara":
            path = require_pulsara_home()
        else:
            raise ValueError("unknown capability root")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return {"path": str(path), "reveal": False}

    async def _reload_live_capabilities(self) -> tuple[int, int]:
        async with self._lock:
            handles = tuple(self._by_session.values())
        if not handles:
            return 0, 0
        results = await asyncio.gather(
            *(
                handle.session.reload_capabilities(deadline_monotonic=None)
                for handle in handles
            ),
            return_exceptions=True,
        )
        attention = sum(
            1
            for result in results
            if isinstance(result, BaseException)
            or (isinstance(result, dict) and result.get("status") != "RELOADED")
        )
        return len(results) - attention, attention

    async def _mark_capability_refresh(self, workspace_root: Path | None = None) -> int:
        return await self.core.mark_capability_change(workspace_root)


async def _settle_capability_io(operation):
    """Keep the mutation lane until a non-cancellable filesystem worker settles."""
    task = asyncio.create_task(operation)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    return task.result()


def _hook_inspection_request(deadline):
    from pulsara_agent.plugins.contracts import InspectLocalPluginsRequest

    return InspectLocalPluginsRequest(deadline)
