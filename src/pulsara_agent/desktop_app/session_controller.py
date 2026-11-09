"""Cold-safe Session control plane for the desktop application."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Literal
from uuid import uuid4

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.capability.workspace_mcp_trust import workspace_mcp_server_approvals
from pulsara_agent.conversation_kernel.host import (
    HostSessionCloseAttempt,
    KernelHostCore,
    KernelHostCoreClosing,
    KernelHostSession,
    KernelRuntimeReopenQuiescence,
    KernelSessionSettlementUnconfirmed,
    KernelSessionSummary,
    SessionControlRejected,
)
from pulsara_agent.conversation_kernel.repository_errors import (
    SessionDeletionBusy,
    SessionWriterConflict,
)
from pulsara_agent.conversation_kernel.session_deletion import (
    KernelSessionRetirement,
    SessionDeleteRejected,
)
from pulsara_agent.desktop_app.capability_projection import (
    SessionWorkspaceKind as SessionWorkspaceKind,
    _capability_payload as _capability_payload,
    _diagnostic_message as _diagnostic_message,
    _mcp_source_label as _mcp_source_label,
    _mcp_transport_payload as _mcp_transport_payload,
    _plugin_connection_editors as _plugin_connection_editors,
    _plugin_enablement_operation as _plugin_enablement_operation,
    _plugin_install_operation as _plugin_install_operation,
    _plugin_removal_operation as _plugin_removal_operation,
    _public_plugin_operation as _public_plugin_operation,
    _public_user_capabilities as _public_user_capabilities,
    _skill_install_payload as _skill_install_payload,
    _skill_issue_payload as _skill_issue_payload,
    _user_capability_payload as _user_capability_payload,
    _user_plugins_payload as _user_plugins_payload,
    _workspace_adoption_payload as _workspace_adoption_payload,
)
from pulsara_agent.desktop_app.path_completion import complete_workspace_paths
from pulsara_agent.desktop_app.session_capabilities import (
    SessionCapabilities,
    _hook_inspection_request as _hook_inspection_request,
    _settle_capability_io as _settle_capability_io,
    _settled_capability_mutation as _settled_capability_mutation,
)
from pulsara_agent.desktop_app.skill_inventory import (
    _absolute_local_source as _absolute_local_source,
    _check_skill_target_collisions as _check_skill_target_collisions,
    _inspect_loose_skill_management as _inspect_loose_skill_management,
    _inspect_user_skills as _inspect_user_skills,
    _inspect_workspace_skills as _inspect_workspace_skills,
    _skill_removal_identity as _skill_removal_identity,
    _workspace_skill_path as _workspace_skill_path,
)
from pulsara_agent.identity import random_id
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    model_call_binding_to_dict,
)
from pulsara_agent.terminal_protocol.canonical_v3 import (
    MAXIMUM_CONTROL_ITEMS,
    MAXIMUM_HISTORY_PAGE_BYTES,
    CanonicalProtocolReader,
    browser_history_uint64,
)
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.tool_permission import EffectivePermissionPolicy
from pulsara_agent.workspace_identity import (
    HostWorkspaceInput,
    observe_workspace,
    resolve_workspace,
)


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


_NO_LIVE_HOST_SEAL = object()
_RUNTIME_REOPEN_OPERATION_SEAL = object()
_RUNTIME_REOPEN_OUTCOME_SEAL = object()
_RAW_CLOSE_OPERATION_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class StableNoLiveHostObservation:
    session_id: str
    operation_nonce: str
    _owner: "LocalSessionController"
    _consumed: bool

    def __init__(
        self,
        *,
        session_id: str,
        operation_nonce: str,
        owner: "LocalSessionController",
        _seal: object,
    ) -> None:
        if _seal is not _NO_LIVE_HOST_SEAL or not session_id or not operation_nonce:
            raise TypeError("no-live Host observation is controller-issued")
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "operation_nonce", operation_nonce)
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_consumed", False)

    def _consume(self, owner: "LocalSessionController") -> None:
        if self._owner is not owner or self._consumed:
            raise RuntimeError("no-live Host observation is stale")
        object.__setattr__(self, "_consumed", True)


@dataclass(frozen=True, slots=True, init=False)
class PreparedRuntimeReopenOperation:
    session_id: str
    old_handle: HostSessionHandle
    operation_nonce: str
    host_quiescence: KernelRuntimeReopenQuiescence
    _owner: "LocalSessionController"

    def __init__(
        self,
        *,
        session_id: str,
        old_handle: HostSessionHandle,
        operation_nonce: str,
        host_quiescence: KernelRuntimeReopenQuiescence,
        owner: "LocalSessionController",
        _seal: object,
    ) -> None:
        if (
            _seal is not _RUNTIME_REOPEN_OPERATION_SEAL
            or old_handle.session_id != session_id
            or host_quiescence.session_id != session_id
            or host_quiescence.host_session_id != old_handle.host_session_id
            or not operation_nonce
        ):
            raise TypeError("runtime-reopen operation is controller-issued")
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "old_handle", old_handle)
        object.__setattr__(self, "operation_nonce", operation_nonce)
        object.__setattr__(self, "host_quiescence", host_quiescence)
        object.__setattr__(self, "_owner", owner)


@dataclass(frozen=True, slots=True, init=False)
class PreparedRuntimeResumeOperation:
    session_id: str
    operation_nonce: str
    _owner: "LocalSessionController"

    def __init__(
        self,
        *,
        session_id: str,
        operation_nonce: str,
        owner: "LocalSessionController",
        _seal: object,
    ) -> None:
        if _seal is not _RUNTIME_REOPEN_OPERATION_SEAL or not operation_nonce:
            raise TypeError("runtime-resume operation is controller-issued")
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "operation_nonce", operation_nonce)
        object.__setattr__(self, "_owner", owner)


RuntimeReopenOperation = PreparedRuntimeReopenOperation | PreparedRuntimeResumeOperation


@dataclass(frozen=True, slots=True, init=False)
class PreparedRawCloseOperation:
    session_id: str
    handle: HostSessionHandle
    operation_nonce: str
    _owner: "LocalSessionController"

    def __init__(
        self,
        *,
        session_id: str,
        handle: HostSessionHandle,
        operation_nonce: str,
        owner: "LocalSessionController",
        _seal: object,
    ) -> None:
        if (
            _seal is not _RAW_CLOSE_OPERATION_SEAL
            or handle.session_id != session_id
            or not operation_nonce
        ):
            raise TypeError("raw-close operation is controller-issued")
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "handle", handle)
        object.__setattr__(self, "operation_nonce", operation_nonce)
        object.__setattr__(self, "_owner", owner)


@dataclass(frozen=True, slots=True, init=False)
class OldHostCloseFull:
    operation: PreparedRuntimeReopenOperation
    settlement_unconfirmed: bool

    def __init__(self, operation: PreparedRuntimeReopenOperation, *, _seal: object, settlement_unconfirmed: bool = False):
        if _seal is not _RUNTIME_REOPEN_OUTCOME_SEAL:
            raise TypeError("old-Host close outcome is controller-issued")
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "settlement_unconfirmed", settlement_unconfirmed)


@dataclass(frozen=True, slots=True, init=False)
class OldHostCloseQuarantined:
    operation: PreparedRuntimeReopenOperation
    public_code: str

    def __init__(
        self,
        operation: PreparedRuntimeReopenOperation,
        *,
        public_code: str,
        _seal: object,
    ) -> None:
        if _seal is not _RUNTIME_REOPEN_OUTCOME_SEAL or not public_code:
            raise TypeError("old-Host quarantine outcome is controller-issued")
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "public_code", public_code)


@dataclass(frozen=True, slots=True, init=False)
class NoOldHostReadyToResume:
    operation: PreparedRuntimeResumeOperation

    def __init__(self, operation: PreparedRuntimeResumeOperation, *, _seal: object):
        if _seal is not _RUNTIME_REOPEN_OUTCOME_SEAL:
            raise TypeError("no-old-Host outcome is controller-issued")
        object.__setattr__(self, "operation", operation)


RuntimeReopenHostOutcome = (
    OldHostCloseFull | OldHostCloseQuarantined | NoOldHostReadyToResume
)


@dataclass(slots=True)
class _ResumeInFlight:
    task: asyncio.Task[HostSessionHandle]
    core_operation: asyncio.Future[None] | None = None


@dataclass(slots=True)
class _WorkspaceRestoreInFlight(_ResumeInFlight):
    pass


@dataclass(slots=True)
class _RawCloseInFlight:
    operation: PreparedRawCloseOperation
    physical_close_full: bool = False
    core_operation: asyncio.Future[None] | None = None
    task: asyncio.Task[None] | None = None
    close_attempt: HostSessionCloseAttempt | None = None


@dataclass(slots=True)
class _RuntimeReopenInFlight:
    operation: RuntimeReopenOperation
    abort_requested: bool = False
    core_operation: asyncio.Future[None] | None = None


@dataclass(frozen=True, slots=True)
class _Quarantined:
    public_code: str
    core_operation: asyncio.Future[None] | None = None


@dataclass(eq=False, slots=True)
class SessionRetirementOperation:
    session_id: str
    owner: "LocalSessionController"
    task: asyncio.Task | None = None
    prior_resume: asyncio.Task | None = None
    core_operation: KernelSessionRetirement | None = None
    detach: object | None = None
    unconfirmed: bool = False
    action: Literal['delete', 'delete_archived', 'archive', 'unarchive'] = 'delete'
    archive_ready: bool = False


_SessionOperation = (
    _ResumeInFlight | _RawCloseInFlight | _RuntimeReopenInFlight | _Quarantined | SessionRetirementOperation
)


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


class LocalSessionController(SessionCapabilities):
    """Own create/resume publication while preserving Kernel close authority."""

    def __init__(
        self,
        *,
        core: KernelHostCore,
        memory_domain_id: str = "u_local",
        trust_workspace_mcp_config: bool = False,
        permission_policy: EffectivePermissionPolicy,
        active_skill_names: frozenset[str],
    ) -> None:
        self.core = core
        self.memory_domain_id = memory_domain_id
        self.trust_workspace_mcp_config = trust_workspace_mcp_config
        self.permission_policy = permission_policy
        self.active_skill_names = active_skill_names
        self._by_session: dict[str, HostSessionHandle] = {}
        self._by_host: dict[str, HostSessionHandle] = {}
        self._operations: dict[str, _SessionOperation] = {}
        self._forks: set[asyncio.Task[dict[str, object]]] = set()
        self._fork_sessions: dict[asyncio.Task, frozenset[str]] = {}
        self._lock = asyncio.Lock()
        self._capability_mutation_lock = core.mcp_management.lane
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    async def prepare(self) -> None:
        """Verify cold resources without activating a HostSession."""

        await self.core.prepare_resources()

    def bootstrap_payload(self) -> dict[str, object]:
        return {
            "application": {
                "name": "Pulsara",
                "version": "0.1.3",
                "transport": "localhost-http+terminal-v3",
            },
            "protocol": {"major": 3, "minor": 0},
        }

    async def list_sessions(self) -> list[dict[str, object]]:
        summaries = await self.core.list_resumable_sessions_across_workspaces(
            memory_domain_id=self.memory_domain_id,
            include_archived=False,
        )
        async with self._lock:
            live_ids = frozenset(self._by_session)
            handles = dict(self._by_session)
            busy_ids = frozenset(self._operations)
        idle_ids = await self.core.idle_session_ids(self.memory_domain_id)
        result = []
        for item in summaries:
            payload = self._summary_payload(item, item.session_id in live_ids)
            idle = item.session_id in idle_ids and item.session_id not in busy_ids
            if idle and item.session_id in handles:
                idle = await handles[item.session_id].session.inspect_archive_idle()
            payload['can_archive'] = idle
            result.append(payload)
        return result

    async def search_sessions(self, *, query: str, lifecycle: str = "ALL", cursor: str | None = None, limit: int = 20):
        from pulsara_agent.conversation_kernel.host import _kernel_session_summary
        rows, next_cursor = await self.core.search_sessions(
            memory_domain_id=self.memory_domain_id,
            query=query, lifecycle=lifecycle, cursor=cursor, limit=limit)
        return {"items": [{"session": self._summary_payload(_kernel_session_summary(row), False),
                           "match_kind": row["match_kind"], "snippet": row["snippet"]} for row in rows],
                "next_cursor": next_cursor}

    async def rename_session(self, session_id: str, title: str):
        async with self._lock:
            if self._closing:
                raise SessionControlRejected("SESSION_RENAME_UNAVAILABLE", "服务正在关闭，请稍后重试。")
        saved = await self.core.rename_session(
            session_id, memory_domain_id=self.memory_domain_id, title=title,
        )
        return {"session_id": session_id, "title": saved}

    async def list_archived_sessions(self):
        summaries = await self.core.list_resumable_sessions_across_workspaces(
            memory_domain_id=self.memory_domain_id, include_archived=True,
        )
        return [self._summary_payload(item, False) for item in summaries if item.lifecycle == 'ARCHIVED']

    async def archive_session(self, session_id: str, *, bridge):
        return await self.delete_session(session_id, bridge=bridge, action='archive')

    async def unarchive_session(self, session_id: str):
        async with self._lock:
            current = self._operations.get(session_id)
            if self._closing or (current is not None and not (
                isinstance(current, SessionRetirementOperation) and current.action == 'unarchive'
            )):
                raise SessionControlRejected('SESSION_ARCHIVE_BUSY', '会话正在处理另一项操作。')
            operation = current or SessionRetirementOperation(session_id, self, action='unarchive')
            if operation.task is None or (operation.task.done() and operation.unconfirmed):
                self._operations[session_id] = operation
                operation.task = asyncio.create_task(self._unarchive_owner(operation))
        return await asyncio.shield(operation.task)

    async def _unarchive_owner(self, operation):
        try:
            if not operation.unconfirmed:
                try:
                    await self.core.unarchive_session(operation.session_id, self.memory_domain_id)
                    return {'status': 'OPEN', 'session_id': operation.session_id}
                except KeyError:
                    raise
                except Exception:
                    operation.unconfirmed = True
            try:
                status = await self.core.read_session_lifecycle(operation.session_id, self.memory_domain_id)
            except Exception as exc:
                raise SessionDeleteRejected('SESSION_ARCHIVE_UNCONFIRMED', '暂时无法确认操作结果，请恢复数据库连接后重试确认。', 503) from exc
            operation.unconfirmed = False
            if status is None:
                raise KeyError(operation.session_id)
            if status != 'OPEN':
                raise SessionDeleteRejected('SESSION_ARCHIVE_FAILED', '取消归档未完成，可以重试。', 500)
            return {'status': 'OPEN', 'session_id': operation.session_id}
        finally:
            async with self._lock:
                if not operation.unconfirmed and self._operations.get(operation.session_id) is operation:
                    self._operations.pop(operation.session_id)


    async def _session_capability_payload(
        self, handle: HostSessionHandle
    ) -> dict[str, object]:
        workspace_root = handle.session.workspace.workspace_root
        workspace_skills, workspace_mcp = await asyncio.gather(
            asyncio.to_thread(_inspect_workspace_skills, workspace_root),
            asyncio.to_thread(self.core.mcp_management.inspect_scope, workspace_root),
        )
        workspace_mcp_approvals = await asyncio.to_thread(
            workspace_mcp_server_approvals, workspace_root
        )
        return _capability_payload(
            session_id=handle.session_id,
            workspace_root=workspace_root,
            workspace_kind=(
                "quick"
                if handle.session.workspace.workspace_kind == "transient"
                else "project"
            ),
            workspace_skills=workspace_skills,
            workspace_mcp_configs=workspace_mcp,
            trust_all_workspace_mcp=(
                handle.session.workspace.trust_workspace_mcp_config
            ),
            workspace_mcp_approvals=workspace_mcp_approvals,
            refresh_pending=handle.session.capability_refresh_pending,
            refresh_attention=handle.session.capability_refresh_attention,
            inspection=handle.session.inspect_capability_catalog(),
        )


    async def create_session(
        self,
        *,
        workspace_kind: SessionWorkspaceKind,
        workspace_path: str | None = None,
        model_call_binding: ModelCallBinding | None = None,
    ) -> HostSessionHandle:
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Desktop application is draining")
        if model_call_binding is not None:
            # Initial selection uses the ordinary model owner, before allocating
            # a quick workspace. It grants no control over an existing session.
            try:
                self.core._model_runtime.freeze_resolution_snapshot().reconcile(
                    model_call_binding
                )
            except ValueError as exc:
                raise SessionControlRejected("MODEL_BINDING_INVALID", str(exc)) from exc
        workspace_input = self._workspace_input_for_create(
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
        )
        session = await self.core.open_session(
            workspace_input,
            permission_policy=self.permission_policy,
            active_skill_names=self.active_skill_names,
        )
        handle = HostSessionHandle(session, workspace_input)
        try:
            if model_call_binding is not None:
                try:
                    await session.update_model_call_binding(model_call_binding)
                except ValueError as exc:
                    raise SessionControlRejected("MODEL_BINDING_INVALID", str(exc)) from exc
            async with self._lock:
                if self._closing:
                    raise KernelHostCoreClosing("Desktop application is draining")
                self._publish_locked(handle)
            return handle
        except BaseException:
            await self.core.close_session(
                session.host_session_id
            )
            raise

    async def complete_workspace_paths(self, session_id: str, prefix: str, cursor: str | None) -> dict[str, object]:
        summary = await self.core.read_resumable_session(
            session_id, memory_domain_id=self.memory_domain_id,
        )
        if summary is None:
            raise KeyError(session_id)
        return await asyncio.to_thread(complete_workspace_paths, Path(summary.workspace_root), prefix, cursor)

    async def read_session(self, session_id: str) -> dict[str, object] | None:
        summary = await self.core.read_resumable_session(
            session_id,
            memory_domain_id=self.memory_domain_id,
        )
        return (
            None
            if summary is None
            else self._summary_payload(summary, session_id in self._by_session)
        )

    async def fork_conversation(
        self,
        source_session_id: str,
        *,
        anchor_entry_id: str,
    ) -> dict[str, object]:
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Desktop application is draining")
            if source_session_id in self._operations:
                raise SessionControlRejected("SESSION_CONTROL_BUSY", "会话正在处理另一项操作。")
            child_session_id = random_id("session")
            task = asyncio.create_task(
                self._fork_owner(source_session_id, anchor_entry_id, child_session_id),
                name=f"desktop-fork:{child_session_id}",
            )
            self._forks.add(task)
            self._fork_sessions[task] = frozenset((source_session_id, child_session_id))
            task.add_done_callback(self._forks.discard)
            task.add_done_callback(lambda done: self._fork_sessions.pop(done, None))
        # A disconnected browser must not cancel a commit or turn an open failure
        # into a false NOT_CREATED. Shutdown joins this same settlement owner.
        return await asyncio.shield(task)

    async def _fork_owner(
        self, source_session_id: str, anchor_entry_id: str, child_session_id: str
    ) -> dict[str, object]:
        deadline = self.core._canonical_deadline()
        while True:
            creation = await self.core.fork_conversation(
                source_session_id=source_session_id,
                anchor_entry_id=anchor_entry_id,
                child_session_id=child_session_id,
                memory_domain_id=self.memory_domain_id,
                deadline_monotonic=deadline,
            )
            if creation.public_code != "CHILD_SESSION_ALREADY_EXISTS":
                break
            if monotonic() >= deadline:
                raise TimeoutError("fork allocation deadline expired")
            async with self._lock:
                if self._closing:
                    raise KernelHostCoreClosing("Desktop application is draining")
                child_session_id = random_id("session")
                self._fork_sessions[asyncio.current_task()] = frozenset((source_session_id, child_session_id))
        if not creation.created:
            return {
                "outcome": "NOT_CREATED",
                "child_session_id": child_session_id,
                "public_code": creation.public_code,
            }
        try:
            await self.resume_session(child_session_id)
        except Exception as error:
            return {
                "outcome": "CREATED_OPEN_DEFERRED",
                "child_session_id": child_session_id,
                "public_code": f"CHILD_OPEN_DEFERRED: {error}",
            }
        return {"outcome": "CREATED_AND_OPENED", "child_session_id": child_session_id}

    async def workspace_availability(self, session_id: str) -> dict[str, object]:
        summary = await self.core.read_resumable_session(session_id, memory_domain_id=self.memory_domain_id)
        if summary is None:
            raise KeyError(session_id)
        return observe_workspace(summary.workspace_root).to_dict()

    async def _history_reader(self, session_id: str) -> CanonicalProtocolReader:
        # Authorize every read independently. No writer, Host or browser attachment.
        repository = await self.core._ensure_resources()
        reader = CanonicalProtocolReader(repository.connection_provider)
        await asyncio.to_thread(
            reader.authorize_session,
            session_id=session_id,
            memory_domain_id=self.memory_domain_id,
            deadline_monotonic=self.core._canonical_deadline(),
        )
        return reader

    async def authorize_agent_reader(self, root_session_id: str, agent_session_id: str):
        repository = await self.core._ensure_resources()
        reader = CanonicalProtocolReader(repository.connection_provider)
        await asyncio.to_thread(reader.authorize_agent, root_session_id=root_session_id,
            agent_session_id=agent_session_id, memory_domain_id=self.memory_domain_id,
            deadline_monotonic=self.core._canonical_deadline())
        return reader

    async def _sidebar_root(self, root_session_id: str):
        reader = await self._history_reader(root_session_id)
        root = await asyncio.to_thread(reader.authorize_session, session_id=root_session_id,
            memory_domain_id=self.memory_domain_id, deadline_monotonic=self.core._canonical_deadline())
        if root["root_session_id"] is not None:
            raise KeyError(root_session_id)
        return reader

    async def sidebar_agents(self, root_session_id: str, body: dict):
        if set(body) - {"cursor", "max_items"}:
            raise ValueError("Agent page parameters are invalid")
        await self._sidebar_root(root_session_id)
        repository = await self.core._ensure_resources()
        maximum = browser_history_uint64(body.get("max_items", 50), "Agent page size")
        if not 1 <= maximum <= 50:
            raise ValueError("Agent page size is invalid")
        rows = await asyncio.to_thread(repository.list_query_agent_sessions, root_session_id=root_session_id,
            observer_session_id=root_session_id,
            cursor=body.get("cursor"), limit=maximum + 1, deadline_monotonic=self.core._canonical_deadline())
        selected = rows[:maximum]
        await self._sidebar_root(root_session_id)
        return {"agents": [self._sidebar_agent_value(row) for row in selected],
                "next_cursor": str(selected[-1]["agent_id"]) if len(rows) > maximum else None}

    def _sidebar_agent_value(self, row):
        live = self.core._live_agent_session(str(row["agent_id"]))
        current_work = None if row["ui_current_work_id"] is None else {
            "work_id": row["ui_current_work_id"],
            "work_title": row["ui_current_work_title"],
            "status": row["ui_current_work_status"],
        }
        return {"agent_id": str(row["agent_id"]), "title": row.get("title"),
                "current_work": current_work, "queued_work_count": row["ui_queued_work_count"],
                "has_running_turn": row["has_running_turn"],
                "host_session_id": None if live is None else live.host_session_id}

    async def sidebar_agent(self, root_session_id: str, agent_session_id: str):
        from pulsara_agent.conversation_kernel.presentation_targets import resolve_visible_agent
        await self.authorize_agent_reader(root_session_id, agent_session_id)
        repository = await self.core._ensure_resources()
        try:
            row = await asyncio.to_thread(resolve_visible_agent, repository, root_session_id=root_session_id,
                agent_session_id=agent_session_id, deadline_monotonic=self.core._canonical_deadline())
        except LookupError as error:
            raise KeyError(agent_session_id) from error
        await self.authorize_agent_reader(root_session_id, agent_session_id)
        return {"agent": self._sidebar_agent_value(row)}

    async def sidebar_work(self, root_session_id: str, work_id: str):
        await self._sidebar_root(root_session_id)
        repository = await self.core._ensure_resources()
        value = await asyncio.to_thread(repository.read_agent_work_graph_facts,
            root_session_id=root_session_id, work_id=work_id, deadline_monotonic=self.core._canonical_deadline())
        if value is None:
            raise KeyError(work_id)
        await self._sidebar_root(root_session_id)
        value = await asyncio.to_thread(repository.read_agent_work_graph_facts,
            root_session_id=root_session_id, work_id=work_id, deadline_monotonic=self.core._canonical_deadline())
        if value is None:
            raise KeyError(work_id)
        return {"work": {key: value[key] for key in ("agent_id", "work_id", "work_title", "status", "terminal_reason", "consumed_entry_id", "turn_id")},
                "inputs": value["inputs"]}

    async def _sidebar_root_host(self, root_session_id: str, host_session_id: str):
        await self._sidebar_root(root_session_id)
        host = self.core._sessions.get(host_session_id)
        if (host is None or host.session_id != root_session_id or host.root_session_id is not None
                or host._closing or host._closed):
            raise KeyError(host_session_id)
        return host

    async def sidebar_cells(self, root_session_id: str, host_session_id: str, body: dict):
        if set(body) - {"cursor", "max_items"}:
            raise ValueError("Cell page parameters are invalid")
        host = await self._sidebar_root_host(root_session_id, host_session_id)
        maximum = body.get("max_items", 50)
        if type(maximum) is not int or not 1 <= maximum <= 50:
            raise ValueError("Cell page size is invalid")
        result = host._tools.query_code_cells(cursor=body.get("cursor"), max_items=maximum)
        for item in result["cells"]:
            item["source_exec_attempt_id"] = self._sidebar_cell_capture(
                host, item["target"]["cell_id"]).source_exec_attempt_id
        if await self._sidebar_root_host(root_session_id, host_session_id) is not host:
            raise KeyError(host_session_id)
        return result

    async def sidebar_cell_info(self, root_session_id: str, host_session_id: str, cell_id: str):
        host = await self._sidebar_root_host(root_session_id, host_session_id)
        capture = self._sidebar_cell_capture(host, cell_id)
        if await self._sidebar_root_host(root_session_id, host_session_id) is not host:
            raise KeyError(host_session_id)
        current = self._sidebar_cell_capture(host, cell_id)
        if current.source_exec_attempt_id != capture.source_exec_attempt_id:
            raise KeyError(cell_id)
        return {"cell": {**current.public_value(),
                         "source_exec_attempt_id": current.source_exec_attempt_id}}

    async def cell_source(self, root_session_id: str, source_exec_attempt_id: str, body: dict):
        if set(body) - {"offset_chars", "max_chars"}:
            raise ValueError("Cell source parameters are invalid")
        offset = browser_history_uint64(body.get("offset_chars", 0), "Cell source offset")
        maximum = browser_history_uint64(body.get("max_chars", 20000), "Cell source page size")
        if not 1 <= maximum <= 32000:
            raise ValueError("Cell source page size is invalid")
        await self._sidebar_root(root_session_id)
        repository = await self.core._ensure_resources()
        source = await asyncio.to_thread(repository.read_code_mode_source,
            session_id=root_session_id, attempt_id=source_exec_attempt_id,
            deadline_monotonic=self.core._canonical_deadline())
        await self._sidebar_root(root_session_id)
        if source is None:
            raise KeyError(source_exec_attempt_id)
        text = source[offset:offset + maximum]
        end = offset + len(text)
        return {"source_exec_attempt_id": source_exec_attempt_id,
            "live_cell": self._live_cell_for_source(root_session_id, source_exec_attempt_id), "source": {
            "status": "AVAILABLE", "text": text, "offset_chars": offset,
            "returned_chars": len(text), "total_chars": len(source),
            "has_more": end < len(source), "next_offset_chars": end if end < len(source) else None}}

    def _live_cell_for_source(self, root_session_id: str, source_exec_attempt_id: str):
        from pulsara_agent.code_mode.contracts import CodeModeError
        for host in tuple(self.core._sessions.values()):
            if (host.session_id != root_session_id or host.root_session_id is not None
                    or host._closing or host._closed):
                continue
            cursor = None
            while True:
                try:
                    page = host._tools.query_code_cells(cursor=cursor, max_items=50)
                    for item in page["cells"]:
                        cell_id = item["target"]["cell_id"]
                        capture = host._tools.capture_code_cell(cell_id)
                        if capture.source_exec_attempt_id == source_exec_attempt_id:
                            return {"host_session_id": host.host_session_id, "cell_id": cell_id}
                except CodeModeError:
                    # Canonical source reading never depends on live runtime
                    # availability, and this observation does not revive it.
                    break
                cursor = page["next_cursor"]
                if cursor is None:
                    break
        return None

    async def sidebar_cell_graph(self, root_session_id: str, host_session_id: str,
                                 cell_id: str, body: dict):
        if set(body) - {"section", "cursor", "max_items"}:
            raise ValueError("Cell graph parameters are invalid")
        await self._sidebar_root(root_session_id)
        closed = {"status": "CLOSED", "message": "实时工作流视图已关闭"}
        try:
            host = await self._sidebar_root_host(root_session_id, host_session_id)
            capture = self._sidebar_cell_capture(host, cell_id)
        except KeyError:
            await self._sidebar_root(root_session_id)
            return closed
        try:
            result = await host._tools.read_code_cell_graph(cell_id=cell_id,
                section=body.get("section", "works"), cursor=body.get("cursor"),
                max_items=body.get("max_items", 50))
        except Exception:
            # An operation failure alone cannot establish retirement. Check
            # the original live target, and retain a transient error if alive.
            try:
                await self._sidebar_root_host(root_session_id, host_session_id)
                self._sidebar_cell_capture(host, cell_id)
            except KeyError:
                await self._sidebar_root(root_session_id)
                return closed
            raise
        try:
            current_host = await self._sidebar_root_host(root_session_id, host_session_id)
            current = self._sidebar_cell_capture(current_host, cell_id)
            if current_host is not host or current.source_exec_attempt_id != capture.source_exec_attempt_id:
                return closed
        except KeyError:
            # Reauthorize independently: a deleted/inaccessible ROOT is not a
            # retired runtime. Sidecar/network errors keep their real failure.
            await self._sidebar_root(root_session_id)
            return closed
        return result

    @staticmethod
    def _sidebar_cell_capture(host, cell_id):
        from pulsara_agent.code_mode.contracts import CodeModeError
        from pulsara_agent.conversation_kernel.presentation_targets import resolve_visible_cell
        try:
            return resolve_visible_cell(host._tools, cell_id=cell_id)
        except CodeModeError as error:
            if error.code != "unavailable":
                raise
            # A successful inventory distinguishes a retired Cell from an
            # unavailable sidecar; no output observation is reserved.
            host._tools.query_code_cells(max_items=1)
            raise KeyError(cell_id) from error

    async def queue_read(self, session_id: str, body: dict, *, root_session_id: str | None = None):
        from google.protobuf.json_format import MessageToDict
        repository = await self.core._ensure_resources()
        reader = CanonicalProtocolReader(repository.connection_provider)
        common = dict(session_id=session_id, memory_domain_id=self.memory_domain_id,
                      root_session_id=root_session_id, deadline_monotonic=self.core._canonical_deadline())
        if "queue_item_id" in body:
            if set(body) != {"queue_item_id"} or not isinstance(body["queue_item_id"], str):
                raise ValueError("queue item lookup accepts one identity")
            item = await asyncio.to_thread(reader.queue_item, queue_item_id=body["queue_item_id"], **common)
            return {"item": None if item is None else MessageToDict(item, preserving_proto_field_name=True)}
        if set(body) - {"cursor", "max_items", "delivery_mode", "target_turn_id"}:
            raise ValueError("queue page parameters are invalid")
        cursor = browser_history_uint64(body.get("cursor", 0), "queue cursor")
        maximum = browser_history_uint64(body.get("max_items", 50), "queue page size")
        items, next_cursor = await asyncio.to_thread(reader.queue_page, after_sequence=cursor,
            limit=maximum, delivery_mode=body.get("delivery_mode", "NEW_TURN"),
            target_turn_id=body.get("target_turn_id"), **common)
        return {"items": [MessageToDict(item, preserving_proto_field_name=True) for item in items],
                "next_cursor": None if next_cursor is None else str(next_cursor)}

    async def history_snapshot(self, session_id: str):
        reader = await self._history_reader(session_id)
        return wire.ServerFrame(snapshot=wire.SnapshotResponse(snapshot=await asyncio.to_thread(
            reader.snapshot, session_id=session_id, maximum_entries=256,
            maximum_control_items=MAXIMUM_CONTROL_ITEMS,
            deadline_monotonic=self.core._canonical_deadline())))

    async def history_page(self, session_id: str, cursor: dict, maximum_entries: int,
                           maximum_serialized_bytes: int = MAXIMUM_HISTORY_PAGE_BYTES):
        if cursor.get("session_id") != session_id:
            raise ValueError("history cursor session conflicts")
        if "event_sequence_cut" not in cursor:
            raise ValueError("history cursor lacks its event cut")
        reader = await self._history_reader(session_id)
        response = await asyncio.to_thread(
            reader.history_page, session_id=session_id,
            event_sequence_cut=browser_history_uint64(cursor["event_sequence_cut"], "history event cut"),
            cut_sequence=browser_history_uint64(cursor.get("cut_sequence"), "history cut"),
            before_entry_sequence=browser_history_uint64(cursor.get("entry_sequence"), "history edge"),
            maximum_entries=maximum_entries, maximum_serialized_bytes=maximum_serialized_bytes,
            deadline_monotonic=self.core._canonical_deadline())
        return wire.ServerFrame(history_page=response)

    async def history_window(self, session_id: str, body: dict):
        if "cursor" in body and not isinstance(body["cursor"], dict):
            raise ValueError("history window cursor must be an object")
        reader = await self._history_reader(session_id)
        return await asyncio.to_thread(
            reader.browser_history_window, session_id=session_id,
            memory_domain_id=self.memory_domain_id,
            anchor_entry_id=body.get("anchor_entry_id"), cursor=body.get("cursor"),
            maximum_entries=browser_history_uint64(body.get("maximum_entries", 128), "history entries"),
            maximum_serialized_bytes=browser_history_uint64(
                body.get("maximum_serialized_bytes", MAXIMUM_HISTORY_PAGE_BYTES), "history bytes"),
            deadline_monotonic=self.core._canonical_deadline(),
        )

    async def history_context(self, session_id: str, body: dict):
        if "boundary_event_ids" in body and not isinstance(body["boundary_event_ids"], list):
            raise ValueError("history context boundary event IDs must be an array")
        if ("cut_sequence" in body) != ("event_sequence_cut" in body):
            raise ValueError("history context requires both cuts or neither")
        cuts = ({
            "cut_sequence": browser_history_uint64(body["cut_sequence"], "history cut"),
            "event_sequence_cut": browser_history_uint64(body["event_sequence_cut"], "history event cut"),
        } if "cut_sequence" in body else {})
        reader = await self._history_reader(session_id)
        return await asyncio.to_thread(
            reader.browser_history_context, session_id=session_id,
            memory_domain_id=self.memory_domain_id, entry_ids=body.get("entry_ids"),
            boundary_event_ids=body.get("boundary_event_ids"),
            maximum_serialized_bytes=browser_history_uint64(
                body.get("maximum_serialized_bytes", MAXIMUM_HISTORY_PAGE_BYTES), "history bytes"),
            deadline_monotonic=self.core._canonical_deadline(), **cuts,
        )

    async def history_content(self, session_id: str, request, *, tool_output: bool = False):
        from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
        reader = await self._history_reader(session_id)
        repository = await self.core._ensure_resources()
        method = (TerminalKernelProtocolServer.read_canonical_tool_output if tool_output
                  else TerminalKernelProtocolServer.read_canonical_content)
        return await method(connection_provider=repository.connection_provider,
                            protocol_reader=reader, session_id=session_id, request=request)

    async def restore_missing_workspace(self, session_id: str) -> HostSessionHandle:
        summary = await self.core.read_resumable_session(session_id, memory_domain_id=self.memory_domain_id)
        if summary is None:
            raise KeyError(session_id)
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Desktop application is draining")
            current = self._operations.get(session_id)
            if isinstance(current, _WorkspaceRestoreInFlight):
                task = current.task
            else:
                if current is not None:
                    raise SessionControlRejected("SESSION_CONTROL_BUSY", "会话正在处理另一项操作。")
                core_operation = await self.core.reserve_session_operation(session_id)
                live = self._by_session.get(session_id)
                task = asyncio.create_task(
                    self._restore_live_owner(session_id, live) if live is not None
                    else self._resume_owner(session_id, restore_missing_workspace=True),
                    name=f"desktop-workspace-restore:{session_id}")
                self._operations[session_id] = _WorkspaceRestoreInFlight(task, core_operation)
        return await asyncio.shield(task)

    async def _restore_live_owner(self, session_id: str, handle: HostSessionHandle) -> HostSessionHandle:
        async with self._lock:
            operation = self._operations[session_id]
            core_operation = operation.core_operation
        try:
            await handle.session.restore_missing_workspace()
            return handle
        finally:
            async with self._lock:
                current = self._operations.get(session_id)
                if isinstance(current, _WorkspaceRestoreInFlight) and current.task is asyncio.current_task():
                    self._operations.pop(session_id, None)
            if core_operation is not None:
                await self.core.release_session_operation(core_operation)

    async def resume_session(
        self,
        session_id: str,
        *,
        no_live_observation: StableNoLiveHostObservation | None = None,
    ) -> HostSessionHandle:
        if not session_id:
            raise ValueError("session_id is required")
        summary = None
        if no_live_observation is None:
            summary = await self.core.read_resumable_session(
                session_id,
                memory_domain_id=self.memory_domain_id,
            )
            if summary is None:
                raise KeyError(session_id)
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Desktop application is draining")
            live = self._by_session.get(session_id)
            current = self._operations.get(session_id)
            if isinstance(current, _Quarantined):
                raise SessionControlRejected(
                    current.public_code,
                    "会话仍在停止，当前只能查看已保存的历史。",
                )
            if live is not None:
                if no_live_observation is not None or current is not None:
                    raise RuntimeError("Session has another current operation")
                # UI publication is a handle cache. Core decides whether its
                # execution owner is reusable, stopping or requires recovery.
                task = asyncio.create_task(
                    self._resume_owner(session_id),
                    name=f"desktop-resume:{session_id}",
                )
                self._operations[session_id] = _ResumeInFlight(task)
                self._by_session.pop(session_id, None)
                self._by_host.pop(live.host_session_id, None)
            elif isinstance(current, _ResumeInFlight):
                if no_live_observation is not None:
                    raise RuntimeError("runtime reopen observation raced with resume")
                task = current.task
            else:
                if no_live_observation is not None:
                    if (
                        not isinstance(current, _RuntimeReopenInFlight)
                        or no_live_observation.session_id != session_id
                        or no_live_observation.operation_nonce
                        != current.operation.operation_nonce
                    ):
                        raise RuntimeError("runtime reopen observation is not current")
                    no_live_observation._consume(self)
                else:
                    if current is not None:
                        raise RuntimeError("Session has another current operation")
                    self._issue_no_live_observation_locked(session_id)._consume(self)
                core_operation = current.core_operation if isinstance(current, _RuntimeReopenInFlight) else None
                task = asyncio.create_task(
                    self._resume_owner(session_id),
                    name=f"desktop-resume:{session_id}",
                )
                self._operations[session_id] = _ResumeInFlight(task, core_operation)
        return await asyncio.shield(task)

    def host_writer_unavailable(self, host_session_id: str) -> bool:
        handle = self._by_host.get(host_session_id)
        return handle is not None and bool(getattr(handle.session, "writer_unavailable", False))

    async def runtime_closing(self, session_id: str) -> bool:
        async with self._lock:
            current = self._operations.get(session_id)
            return isinstance(current, (_RawCloseInFlight, _Quarantined)) or self.core.session_runtime_closing(session_id)

    async def _resume_owner(self, session_id: str, *, restore_missing_workspace: bool = False) -> HostSessionHandle:
        task = asyncio.current_task()
        async with self._lock:
            current = self._operations.get(session_id)
            core_operation = current.core_operation if isinstance(current, _ResumeInFlight) else None
        try:
            summary = await self.core.read_resumable_session(
                session_id,
                memory_domain_id=self.memory_domain_id,
            )
            if summary is None:
                raise KeyError(session_id)
            workspace_input = HostWorkspaceInput(
                workspace_kind=(
                    "project" if summary.workspace_kind == "project" else "transient"
                ),
                workspace_root=summary.workspace_root,
                display_label=(
                    None
                    if summary.workspace_kind == "project"
                    else summary.workspace_label
                ),
                memory_domain_id=summary.memory_domain_id,
                cleanup_workspace_root_on_close=False,
                trust_workspace_mcp_config=(
                    self.trust_workspace_mcp_config
                ),
            )
            session = await self.core.resume_session(
                session_id,
                workspace_input=workspace_input,
                permission_policy=self.permission_policy,
                active_skill_names=self.active_skill_names,
                restore_missing_workspace=restore_missing_workspace,
                operation=core_operation,
            )
            handle = HostSessionHandle(session, workspace_input)
            try:
                raced: HostSessionHandle | None = None
                async with self._lock:
                    if self._closing:
                        raise KernelHostCoreClosing("Desktop application is draining")
                    current = self._operations.get(session_id)
                    deleting = isinstance(current, SessionRetirementOperation) and current.prior_resume is task
                    if not deleting and (
                        not isinstance(current, _ResumeInFlight) or current.task is not task
                    ):
                        raise RuntimeError("Session resume is no longer current")
                    raced = self._by_session.get(session_id)
                    if raced is not None:
                        raise RuntimeError("Session resume raced with live publication")
                    if not deleting:
                        self._operations.pop(session_id, None)
                    self._by_session[handle.session_id] = handle
                    self._by_host[handle.host_session_id] = handle
                if raced is not None:
                    await self.core.close_session(
                        session.host_session_id
                    )
                    return raced
                return handle
            except BaseException:
                await self.core.close_session(
                    session.host_session_id
                )
                raise
        finally:
            async with self._lock:
                current = self._operations.get(session_id)
                if isinstance(current, _ResumeInFlight) and current.task is task:
                    self._operations.pop(session_id, None)
            if core_operation is not None:
                await self.core.release_session_operation(core_operation)

    def _issue_no_live_observation_locked(
        self, session_id: str
    ) -> StableNoLiveHostObservation:
        if (
            self._by_session.get(session_id) is not None
            or self._operations.get(session_id) is not None
        ):
            raise RuntimeError("Session does not have a stable no-live state")
        return StableNoLiveHostObservation(
            session_id=session_id,
            operation_nonce=random_id("operation"),
            owner=self,
            _seal=_NO_LIVE_HOST_SEAL,
        )

    def _publish_locked(self, handle: HostSessionHandle) -> None:
        if handle.session_id in self._operations:
            raise SessionControlRejected("SESSION_CONTROL_BUSY", "会话正在处理另一项操作。")
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
                memory_domain_id=self.memory_domain_id,
                cleanup_workspace_root_on_close=False,
                trust_workspace_mcp_config=False,
            )
        elif workspace_kind == "project":
            if workspace_path is None or not workspace_path.strip():
                raise ValueError("project workspace_path is required")
            candidate = Path(workspace_path).expanduser()
            if not candidate.is_absolute():
                raise ValueError("project workspace_path must be absolute")
            workspace_input = HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=candidate,
                display_label=None,
                memory_domain_id=self.memory_domain_id,
                cleanup_workspace_root_on_close=False,
                trust_workspace_mcp_config=(
                    self.trust_workspace_mcp_config
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
        if handle is not None:
            return handle.session
        # Agent Hosts are owned by Core's scheduler, not Desktop's ROOT map.
        session = self.core._sessions.get(host_session_id)
        if session is None or session.root_session_id is None or session._closing or session._closed:
            raise KeyError(host_session_id)
        return session

    async def read_context_usage(self, session_id: str) -> dict[str, object]:
        # This advisory GET must not own session activation or join a lifecycle
        # operation. Opening the conversation owns hooks, MCP and Host startup.
        async with self._lock:
            if self._closing:
                return {"state": "unavailable", "connection_id": None}
            if session_id in self._operations:
                return {"state": "updating", "connection_id": None}
            handle = self._by_session.get(session_id)
            if handle is None:
                return {"state": "unavailable", "connection_id": None}
        value = await handle.session.read_context_usage()
        async with self._lock:
            if self._closing or self._by_session.get(session_id) is not handle:
                return {"state": "unavailable", "connection_id": None}
            if session_id in self._operations:
                return {"state": "updating", "connection_id": None}
        return value

    async def invalidate_input_suggestions(self) -> None:
        # User-level preference: retire suggestions in every existing Host.
        async with self._lock:
            for handle in self._by_session.values():
                handle.session.invalidate_input_suggestion()

    async def read_ephemeral_results(self, session_id: str) -> dict[str, object]:
        # Permission checked against user-level data, without activating a Host.
        summary = await self.core.read_resumable_session(
            session_id, memory_domain_id=self.memory_domain_id,
        )
        if summary is None:
            raise KeyError(session_id)
        async with self._lock:
            handle = self._by_session.get(session_id)
            if not self._closing and session_id not in self._operations and handle is not None:
                return {**handle.session.read_ephemeral_results(),
                        "mcp_readiness": handle.session.mcp_readiness_progress()}
        return {
            'session_id': session_id, 'host_session_id': None,
            'suggestion': {'state': 'absent', 'origin_turn_id': None, 'turn_id': None,
                           'final_entry_id': None, 'model_call_binding': None, 'text': None},
            'title': {'state': 'absent'},
            'mcp_readiness': None,
        }

    async def update_model_call_binding(
        self, session_id: str, binding: ModelCallBinding
    ) -> dict[str, object]:
        handle = await self.resume_session(session_id)
        accepted = await handle.session.update_model_call_binding(binding)
        return {
            "model_call_binding": model_call_binding_to_dict(accepted),
            "reasoning_preference_reset": accepted != binding,
        }

    async def delete_session(self, session_id: str, *, bridge, action: Literal['delete', 'delete_archived', 'archive'] = 'delete') -> dict[str, object]:
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Desktop application is draining")
            current = self._operations.get(session_id)
            if isinstance(current, SessionRetirementOperation):
                if current.action != action:
                    raise SessionControlRejected('SESSION_CONTROL_BUSY', '会话正在处理另一项操作。')
                operation = current
                if operation.unconfirmed and operation.task is not None and operation.task.done():
                    operation.task = asyncio.create_task(self._confirm_session_deletion(operation, bridge))
            else:
                if current is not None and not isinstance(current, _ResumeInFlight):
                    raise SessionDeleteRejected(
                        "SESSION_DELETE_QUARANTINED" if isinstance(current, _Quarantined) else "SESSION_DELETE_BUSY",
                        "会话正在处理另一项操作，请等待操作结束；若已隔离，请重启 Pulsara。",
                    )
                operation = SessionRetirementOperation(
                    session_id, self, prior_resume=current.task if isinstance(current, _ResumeInFlight) else None,
                    action=action,
                )
                self._operations[session_id] = operation
                operation.task = asyncio.create_task(
                    self._delete_session_owner(operation, bridge), name=f"session-{action}:{session_id}",
                )
        return await asyncio.shield(operation.task)

    async def confirm_session_retirement_operation(self, operation: SessionRetirementOperation) -> None:
        async with self._lock:
            if operation.owner is not self or self._operations.get(operation.session_id) is not operation:
                raise RuntimeError("session delete operation is stale")

    async def _finish_session_delete(self, operation, bridge, *, quarantine: bool) -> bool:
        from pulsara_agent.desktop_app.renderer_bridge import BridgeSettlementFailed
        if operation.detach is not None:
            try:
                result = await bridge.settle_session_retirement_detach(operation.detach, close_full=not quarantine)
                quarantine = quarantine or isinstance(result, BridgeSettlementFailed)
            except Exception:
                logging.getLogger(__name__).exception("session deletion bridge cleanup failed")
                quarantine = True
            operation.detach = None
        if operation.core_operation is not None:
            try:
                await self.core.finish_session_retirement(operation.core_operation, quarantine=quarantine)
            except Exception:
                logging.getLogger(__name__).exception("session deletion core cleanup failed")
                quarantine = True
        async with self._lock:
            if self._operations.get(operation.session_id) is operation:
                if quarantine:
                    self._operations[operation.session_id] = _Quarantined("SESSION_DELETE_QUARANTINED")
                else:
                    self._operations.pop(operation.session_id)
        return quarantine

    @staticmethod
    def _session_delete_result(operation, status: str, quarantine: bool) -> dict[str, object]:
        result: dict[str, object] = {"status": status, "session_id": operation.session_id}
        if operation.action == 'delete_archived':
            descriptions = {"DELETED": "本条已删除", "ABSENT": "本条已不存在", "NOT_ARCHIVED": "本条已跳过"}
            result["cleanup_warning"] = ({
                "code": "SESSION_DELETE_QUARANTINED",
                "message": descriptions[status] + "，但本地会话准入未能安全释放；请重启 Pulsara 后再操作此会话。",
            } if quarantine else None)
        return result

    async def _confirm_session_deletion(self, operation, bridge) -> dict[str, object]:
        try:
            if operation.action == 'archive':
                lifecycle = await self.core.read_session_lifecycle(operation.session_id, self.memory_domain_id)
                exists = lifecycle != 'ARCHIVED'
            else:
                exists = await self.core.canonical_session_exists(
                    operation.session_id, self.memory_domain_id,
                )
        except Exception as exc:
            operation.unconfirmed = True
            if operation.core_operation is not None:
                await self.core.finish_session_retirement(operation.core_operation, quarantine=True)
            raise SessionDeleteRejected(
                "SESSION_ARCHIVE_UNCONFIRMED" if operation.action == 'archive' else "SESSION_DELETE_UNCONFIRMED",
                "暂时无法确认操作结果，请恢复数据库连接后重试确认。", 503,
            ) from exc
        operation.unconfirmed = False
        quarantine = await self._finish_session_delete(operation, bridge, quarantine=False)
        if exists:
            raise SessionDeleteRejected('SESSION_ARCHIVE_FAILED' if operation.action == 'archive' else "SESSION_DELETE_FAILED", "操作未完成，可以重试。", 500)
        return self._session_delete_result(operation, 'ARCHIVED' if operation.action == 'archive' else "ABSENT", quarantine)

    async def _delete_session_owner(self, operation, bridge) -> dict[str, object]:
        from pulsara_agent.desktop_app.renderer_bridge import BridgeDetachFull, BridgeDetachFailed
        confirmed_status = None
        physical_started = False
        try:
            if operation.prior_resume is not None:
                await asyncio.gather(asyncio.shield(operation.prior_resume), return_exceptions=True)
            async with self._lock:
                pending = tuple(t for t, ids in self._fork_sessions.items() if operation.session_id in ids)
            # A trusted child ID may already be admitted but not yet committed.
            # Join its creator before interpreting an absent canonical row.
            if pending:
                await asyncio.gather(*(asyncio.shield(t) for t in pending), return_exceptions=True)
            # Do not touch another domain's runtime even if an ID is guessed.
            try:
                exists = await self.core.canonical_session_exists(
                    operation.session_id, self.memory_domain_id,
                )
            except Exception as exc:
                if operation.action != 'delete_archived':
                    raise
                raise SessionDeleteRejected('SESSION_DELETE_PREFLIGHT_FAILED', '暂时无法读取会话，本次尚未执行删除，请恢复数据库连接后重试。', 503) from exc
            async with self._lock:
                handle = self._by_session.get(operation.session_id)
            if not exists and handle is None:
                if operation.action == 'archive':
                    await self._finish_session_delete(operation, bridge, quarantine=False)
                    raise KeyError(operation.session_id)
                confirmed_status = 'ABSENT'
                quarantine = await self._finish_session_delete(operation, bridge, quarantine=False)
                return self._session_delete_result(operation, confirmed_status, quarantine)
            if handle is not None and handle.workspace_input.memory_domain_id != self.memory_domain_id:
                raise RuntimeError("session delete domain mismatch")
            operation.core_operation = await self.core.begin_session_retirement(
                operation.session_id, self.memory_domain_id,
            )
            if operation.action == 'delete_archived':
                try:
                    lifecycle = await self.core.read_session_lifecycle(operation.session_id, self.memory_domain_id)
                except Exception as exc:
                    raise SessionDeleteRejected('SESSION_DELETE_PREFLIGHT_FAILED', '暂时无法读取会话，本次尚未执行删除，请恢复数据库连接后重试。', 503) from exc
                if lifecycle != 'ARCHIVED':
                    confirmed_status = 'ABSENT' if lifecycle is None else 'NOT_ARCHIVED'
                    quarantine = await self._finish_session_delete(operation, bridge, quarantine=False)
                    return self._session_delete_result(operation, confirmed_status, quarantine)
            if operation.action == 'archive':
                try:
                    await self.core.prepare_session_archive(operation.core_operation)
                except SessionWriterConflict as exc:
                    raise SessionDeleteRejected(
                        'SESSION_ARCHIVE_BUSY',
                        '会话的运行权已改变，或正被其他 Pulsara 实例使用。请关闭重复实例，重新打开此会话后再归档。',
                    ) from exc
                except (SessionDeletionBusy, RuntimeError) as exc:
                    raise SessionDeleteRejected('SESSION_NOT_IDLE', '会话仍有任务、后台进程或待处理事项，暂时无法归档。') from exc
                operation.archive_ready = True
            physical_started = True
            detach = await bridge.detach_session_for_retirement(operation)
            if isinstance(detach, (BridgeDetachFull, BridgeDetachFailed)):
                operation.detach = detach.token
            if not isinstance(detach, BridgeDetachFull):
                raise SessionDeleteRejected("SESSION_ARCHIVE_QUARANTINED" if operation.action == 'archive' else "SESSION_DELETE_QUARANTINED", "未能安全断开会话，操作尚未完成。")
            await self.core.quiesce_session_retirement(operation.core_operation)
            async with self._lock:
                handle = self._by_session.pop(operation.session_id, None)
                if handle is not None:
                    self._by_host.pop(handle.host_session_id, None)
            try:
                if operation.action == 'archive':
                    status = await self.core.commit_session_archive(operation.core_operation)
                elif operation.action == 'delete_archived':
                    status = await self.core.commit_session_deletion(operation.core_operation, required_lifecycle='ARCHIVED')
                else:
                    status = await self.core.commit_session_deletion(operation.core_operation)
            except SessionDeletionBusy as exc:
                raise SessionDeleteRejected("SESSION_ARCHIVE_BUSY" if operation.action == 'archive' else "SESSION_DELETE_BUSY", "会话的运行权已改变，或仍被另一进程使用，请稍后重试。") from exc
            except Exception:
                logging.getLogger(__name__).exception("session deletion commit needs confirmation")
                return await self._confirm_session_deletion(operation, bridge)
            confirmed_status = status
            quarantine = await self._finish_session_delete(operation, bridge, quarantine=False)
            return self._session_delete_result(operation, status, quarantine)
        except Exception as exc:
            if operation.unconfirmed:
                raise
            if self._operations.get(operation.session_id) is operation:
                quarantine = (operation.core_operation is not None and not operation.core_operation.physical_full
                              and (operation.action != 'archive' or operation.archive_ready)
                              and (operation.action != 'delete_archived' or physical_started))
                quarantine = await self._finish_session_delete(operation, bridge, quarantine=quarantine)
                if (quarantine and isinstance(exc, SessionDeleteRejected)
                    and exc.public_code == 'SESSION_DELETE_PREFLIGHT_FAILED'):
                    raise SessionDeleteRejected('SESSION_DELETE_QUARANTINED', '本次尚未删除，但会话准入未能安全释放；请重启 Pulsara 后重试。') from exc
            if confirmed_status is not None:
                logging.getLogger(__name__).exception("session deleted; local cleanup needs refresh")
                return self._session_delete_result(operation, confirmed_status, True)
            if isinstance(exc, (SessionDeleteRejected, KeyError)):
                raise
            raise SessionDeleteRejected("SESSION_ARCHIVE_QUARANTINED" if operation.action == 'archive' else "SESSION_DELETE_QUARANTINED", "未能安全关闭运行时，操作尚未完成，请重启 Pulsara 后重试。") from exc

    async def request_raw_close(self, session_id: str, *, bridge) -> str:
        """Transfer an accepted close to the existing session operation owner."""
        async def admit():
            operation = await self.prepare_raw_close(session_id)
            if operation is None:
                return "closed"
            async with self._lock:
                current = self._operations.get(session_id)
                if not isinstance(current, _RawCloseInFlight) or current.operation is not operation:
                    raise RuntimeError("raw close admission lost its operation")
                current.task = asyncio.create_task(
                    self._raw_close_owner(operation, bridge),
                    name=f"desktop-session-close:{session_id}",
                )
                current.task.add_done_callback(self._observe_raw_close)
            return "closing"

        admission = asyncio.create_task(admit())
        try:
            return await asyncio.shield(admission)
        except asyncio.CancelledError:
            # Losing an HTTP waiter cannot strand a prepared close reservation.
            while not admission.done():
                try:
                    await asyncio.shield(admission)
                except asyncio.CancelledError:
                    continue
            admission.result()
            raise

    @staticmethod
    def _observe_raw_close(task):
        if not task.cancelled() and (error := task.exception()) is not None:
            logging.getLogger(__name__).warning("Session close did not fully settle", exc_info=error)

    async def _raw_close_owner(self, operation, bridge):
        from pulsara_agent.desktop_app.renderer_bridge import (
            BridgeDetachNotStarted, BridgeDetachFailed, BridgeDetachFull,
            BridgeSettlementFailed,
        )
        session_id = operation.session_id
        detach = await bridge.detach_session_for_raw_close(operation)
        if isinstance(detach, BridgeDetachNotStarted):
            await self.abort_prepared_raw_close(session_id, operation=operation)
            raise SessionControlRejected("SESSION_CLOSE_BUSY", detach.reason)
        if isinstance(detach, BridgeDetachFailed):
            await bridge.quarantine_detach_failure(detach.token, error=detach.error)
            await self.quarantine_raw_close(operation, public_code="SESSION_CLOSE_QUARANTINED")
            raise SessionControlRejected("SESSION_CLOSE_QUARANTINED", "任务仍在停止。")
        assert isinstance(detach, BridgeDetachFull)
        try:
            await self.settle_raw_close(session_id, operation=operation)
        except BaseException:
            await bridge.settle_raw_close_detach(
                detach.token, close_full=operation.handle.session.physical_close_complete,
            )
            raise
        settlement = await bridge.settle_raw_close_detach(detach.token, close_full=True)
        if isinstance(settlement, BridgeSettlementFailed):
            await self.quarantine_raw_close(operation, public_code="SESSION_CLOSE_QUARANTINED")
            raise SessionControlRejected("SESSION_CLOSE_QUARANTINED", "任务仍在停止。")
        await self.finalize_raw_close(operation, bridge_settlement=settlement, bridge_owner=bridge)

    async def prepare_raw_close(self, session_id: str) -> PreparedRawCloseOperation | None:
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Desktop application is draining")
            current = self._operations.get(session_id)
            if current is not None:
                code = (
                    current.public_code
                    if isinstance(current, _Quarantined)
                    else "SESSION_CONTROL_BUSY"
                )
                raise SessionControlRejected(
                    code, "Session has another current operation"
                )
            handle = self._by_session.get(session_id)
            if handle is None:
                return None
            operation = PreparedRawCloseOperation(
                session_id=session_id,
                handle=handle,
                operation_nonce=random_id("operation"),
                owner=self,
                _seal=_RAW_CLOSE_OPERATION_SEAL,
            )
            core_operation = await self.core.reserve_session_operation(session_id)
            close_attempt = await self.core.request_close_session(handle.host_session_id)
            self._operations[session_id] = _RawCloseInFlight(
                operation, core_operation=core_operation, close_attempt=close_attempt,
            )
            return operation

    async def confirm_raw_close_operation(
        self, operation: PreparedRawCloseOperation
    ) -> None:
        async with self._lock:
            current = self._operations.get(operation.session_id)
            if (
                operation._owner is not self
                or not isinstance(current, _RawCloseInFlight)
                or current.operation is not operation
                or self._by_session.get(operation.session_id) is not operation.handle
            ):
                raise SessionControlRejected(
                    "RAW_CLOSE_NOT_CURRENT", "raw close operation is stale"
                )

    async def settle_raw_close(
        self, session_id: str, *, operation: PreparedRawCloseOperation
    ) -> None:
        async with self._lock:
            current = self._operations.get(session_id)
            if (
                not isinstance(current, _RawCloseInFlight)
                or current.operation is not operation
            ):
                raise RuntimeError("raw close operation is stale")
            if self._by_session.get(session_id) is not operation.handle:
                raise RuntimeError("raw close lost its exact live Host")
            self._by_session.pop(session_id, None)
            self._by_host.pop(operation.handle.host_session_id, None)
            close_attempt = current.close_attempt
        try:
            if close_attempt is not None:
                await asyncio.shield(close_attempt.task)
        except BaseException:
            async with self._lock:
                current = self._operations.get(session_id)
                if (
                    isinstance(current, _RawCloseInFlight)
                    and current.operation is operation
                ):
                    if operation.handle.session.physical_close_complete:
                        self._operations.pop(session_id, None)
                        if current.core_operation is not None:
                            await self.core.release_session_operation(current.core_operation)
                    else:
                        self._operations[session_id] = _Quarantined(
                            "SESSION_CLOSE_QUARANTINED", core_operation=current.core_operation
                        )
            raise
        async with self._lock:
            current = self._operations.get(session_id)
            if (
                isinstance(current, _RawCloseInFlight)
                and current.operation is operation
            ):
                current.physical_close_full = True

    async def finalize_raw_close(
        self,
        operation: PreparedRawCloseOperation,
        *,
        bridge_settlement: object,
        bridge_owner: object,
    ) -> None:
        from pulsara_agent.desktop_app.renderer_bridge import BridgeSettlementFull

        async with self._lock:
            current = self._operations.get(operation.session_id)
            if (
                not isinstance(current, _RawCloseInFlight)
                or current.operation is not operation
                or not current.physical_close_full
                or not isinstance(bridge_settlement, BridgeSettlementFull)
                or bridge_settlement._owner is not bridge_owner
                or bridge_settlement._operation is not operation
            ):
                raise RuntimeError("raw close finalization is stale")
            self._operations.pop(operation.session_id, None)
            if current.core_operation is not None:
                await self.core.release_session_operation(current.core_operation)

    async def abort_prepared_raw_close(
        self, session_id: str, *, operation: PreparedRawCloseOperation
    ) -> None:
        async with self._lock:
            current = self._operations.get(session_id)
            if (
                isinstance(current, _RawCloseInFlight)
                and current.operation is operation
                and not current.physical_close_full
                and self._by_session.get(session_id) is operation.handle
            ):
                self._operations.pop(session_id, None)
                if current.core_operation is not None:
                    await self.core.release_session_operation(current.core_operation)

    async def quarantine_raw_close(
        self, operation: PreparedRawCloseOperation, *, public_code: str
    ) -> None:
        async with self._lock:
            current = self._operations.get(operation.session_id)
            if (
                isinstance(current, _RawCloseInFlight)
                and current.operation is operation
            ):
                self._operations[operation.session_id] = _Quarantined(public_code, core_operation=current.core_operation)

    async def prepare_runtime_reopen(self, session_id: str) -> RuntimeReopenOperation:
        if not session_id:
            raise ValueError("session_id is required")
        summary = await self.core.read_resumable_session(
            session_id,
            memory_domain_id=self.memory_domain_id,
        )
        if summary is None:
            raise KeyError(session_id)
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Desktop application is draining")
            current = self._operations.get(session_id)
            if current is not None:
                code = (
                    current.public_code
                    if isinstance(current, _Quarantined)
                    else "RUNTIME_REOPEN_BUSY"
                )
                raise SessionControlRejected(
                    code, "Session has another current operation"
                )
            handle = self._by_session.get(session_id)
            core_operation = await self.core.reserve_session_operation(session_id)
            operation: RuntimeReopenOperation
            if handle is None:
                observation = self._issue_no_live_observation_locked(session_id)
                observation._consume(self)
                operation = PreparedRuntimeResumeOperation(
                    session_id=session_id,
                    operation_nonce=random_id("operation"),
                    owner=self,
                    _seal=_RUNTIME_REOPEN_OPERATION_SEAL,
                )
            else:
                try:
                    quiescence = await handle.session.prepare_safe_runtime_reopen()
                except BaseException as exc:
                    await self.core.release_session_operation(core_operation)
                    if not isinstance(exc, RuntimeError):
                        raise
                    raise SessionControlRejected(
                        "RUNTIME_REOPEN_BUSY",
                        "Session still has accepted process-local work.",
                    ) from exc
                operation = PreparedRuntimeReopenOperation(
                    session_id=session_id,
                    old_handle=handle,
                    operation_nonce=random_id("operation"),
                    host_quiescence=quiescence,
                    owner=self,
                    _seal=_RUNTIME_REOPEN_OPERATION_SEAL,
                )
            self._operations[session_id] = _RuntimeReopenInFlight(operation, core_operation=core_operation)
            return operation

    async def confirm_runtime_reopen_operation(
        self, operation: RuntimeReopenOperation
    ) -> None:
        async with self._lock:
            current = self._operations.get(operation.session_id)
            if (
                operation._owner is not self
                or not isinstance(current, _RuntimeReopenInFlight)
                or current.operation is not operation
                or (
                    isinstance(operation, PreparedRuntimeReopenOperation)
                    and self._by_session.get(operation.session_id)
                    is not operation.old_handle
                )
                or (
                    isinstance(operation, PreparedRuntimeResumeOperation)
                    and self._by_session.get(operation.session_id) is not None
                )
            ):
                raise SessionControlRejected(
                    "RUNTIME_REOPEN_NOT_CURRENT",
                    "runtime-reopen operation is stale",
                )

    async def abort_runtime_reopen(
        self, operation: RuntimeReopenOperation, *, reason: str
    ) -> None:
        del reason
        async with self._lock:
            current = self._operations.get(operation.session_id)
            if (
                not isinstance(current, _RuntimeReopenInFlight)
                or current.operation is not operation
            ):
                raise RuntimeError("runtime-reopen operation is stale")
            if isinstance(operation, PreparedRuntimeReopenOperation):
                if (
                    self._by_session.get(operation.session_id)
                    is not operation.old_handle
                ):
                    raise RuntimeError("runtime-reopen old Host is no longer published")
                await operation.old_handle.session.abort_safe_runtime_reopen(
                    operation.host_quiescence
                )
            self._operations.pop(operation.session_id, None)
            if current.core_operation is not None:
                await self.core.release_session_operation(current.core_operation)

    async def prepare_abort_runtime_reopen(
        self, operation: RuntimeReopenOperation
    ) -> None:
        """Freeze cancellation before old-Host unpublication; keep both gates."""

        async with self._lock:
            current = self._operations.get(operation.session_id)
            if (
                not isinstance(current, _RuntimeReopenInFlight)
                or current.operation is not operation
                or current.abort_requested
                or (
                    isinstance(operation, PreparedRuntimeReopenOperation)
                    and self._by_session.get(operation.session_id)
                    is not operation.old_handle
                )
                or (
                    isinstance(operation, PreparedRuntimeResumeOperation)
                    and self._by_session.get(operation.session_id) is not None
                )
            ):
                raise RuntimeError("runtime-reopen abort is stale")
            current.abort_requested = True

    async def finalize_abort_runtime_reopen(
        self,
        operation: RuntimeReopenOperation,
        *,
        bridge_settlement: object,
        bridge_owner: object,
    ) -> None:
        from pulsara_agent.desktop_app.renderer_bridge import BridgeSettlementFull

        async with self._lock:
            current = self._operations.get(operation.session_id)
            if (
                not isinstance(current, _RuntimeReopenInFlight)
                or current.operation is not operation
                or not current.abort_requested
                or not isinstance(bridge_settlement, BridgeSettlementFull)
                or bridge_settlement._owner is not bridge_owner
                or bridge_settlement._operation is not operation
            ):
                raise RuntimeError("runtime-reopen abort finalization is stale")
            if isinstance(operation, PreparedRuntimeReopenOperation):
                if (
                    self._by_session.get(operation.session_id)
                    is not operation.old_handle
                ):
                    raise RuntimeError("runtime-reopen abort lost its old Host")
                await operation.old_handle.session.abort_safe_runtime_reopen(
                    operation.host_quiescence
                )
            elif self._by_session.get(operation.session_id) is not None:
                raise RuntimeError("runtime-resume abort gained a live Host")
            self._operations.pop(operation.session_id, None)
            if current.core_operation is not None:
                await self.core.release_session_operation(current.core_operation)

    async def prepare_runtime_reopen_close(
        self, operation: RuntimeReopenOperation
    ) -> RuntimeReopenHostOutcome:
        if operation._owner is not self:
            raise RuntimeError("runtime-reopen operation belongs to another owner")
        session_id = operation.session_id
        if isinstance(operation, PreparedRuntimeResumeOperation):
            async with self._lock:
                current = self._operations.get(session_id)
                if (
                    not isinstance(current, _RuntimeReopenInFlight)
                    or current.operation is not operation
                    or current.abort_requested
                    or self._by_session.get(session_id) is not None
                ):
                    raise RuntimeError("runtime-resume operation is stale")
            return NoOldHostReadyToResume(operation, _seal=_RUNTIME_REOPEN_OUTCOME_SEAL)

        async with self._lock:
            current = self._operations.get(session_id)
            if (
                not isinstance(current, _RuntimeReopenInFlight)
                or current.operation is not operation
                or current.abort_requested
                or self._by_session.get(session_id) is not operation.old_handle
            ):
                raise RuntimeError("runtime-reopen operation is stale")
            await operation.old_handle.session.commit_safe_runtime_reopen(
                operation.host_quiescence
            )
            self._by_session.pop(session_id, None)
            self._by_host.pop(operation.old_handle.host_session_id, None)
        try:
            await self.core.close_session(
                operation.old_handle.host_session_id,
            )
        except KernelSessionSettlementUnconfirmed:
            logging.getLogger(__name__).warning("Runtime stopped with unconfirmed canonical settlement", exc_info=True)
            return OldHostCloseFull(operation, _seal=_RUNTIME_REOPEN_OUTCOME_SEAL, settlement_unconfirmed=True)
        except BaseException:
            await self.quarantine_runtime_reopen(
                operation, public_code="RUNTIME_REOPEN_OLD_HOST_CLOSE_QUARANTINED"
            )
            return OldHostCloseQuarantined(
                operation,
                public_code="RUNTIME_REOPEN_OLD_HOST_CLOSE_QUARANTINED",
                _seal=_RUNTIME_REOPEN_OUTCOME_SEAL,
            )
        return OldHostCloseFull(operation, _seal=_RUNTIME_REOPEN_OUTCOME_SEAL)

    async def finalize_runtime_reopen(
        self,
        operation: RuntimeReopenOperation,
        *,
        host_outcome: RuntimeReopenHostOutcome,
        bridge_settlement: object,
        bridge_owner: object,
    ) -> StableNoLiveHostObservation | None:
        from pulsara_agent.desktop_app.renderer_bridge import BridgeSettlementFull

        async with self._lock:
            current = self._operations.get(operation.session_id)
            if (
                not isinstance(current, _RuntimeReopenInFlight)
                or current.operation is not operation
                or current.abort_requested
                or host_outcome.operation is not operation
                or isinstance(host_outcome, OldHostCloseQuarantined)
                or not isinstance(bridge_settlement, BridgeSettlementFull)
                or bridge_settlement._owner is not bridge_owner
                or bridge_settlement._operation is not operation
                or bridge_settlement.session_id != operation.session_id
                or self._by_session.get(operation.session_id) is not None
            ):
                raise RuntimeError("runtime-reopen finalization is stale")
            if isinstance(host_outcome, OldHostCloseFull) and host_outcome.settlement_unconfirmed:
                self._operations.pop(operation.session_id, None)
                if current.core_operation is not None:
                    await self.core.release_session_operation(current.core_operation)
                return None
            return StableNoLiveHostObservation(
                session_id=operation.session_id,
                operation_nonce=operation.operation_nonce,
                owner=self,
                _seal=_NO_LIVE_HOST_SEAL,
            )

    async def quarantine_runtime_reopen(
        self, operation: RuntimeReopenOperation, *, public_code: str
    ) -> None:
        async with self._lock:
            current = self._operations.get(operation.session_id)
            if (
                isinstance(current, _RuntimeReopenInFlight)
                and current.operation is operation
            ):
                self._operations[operation.session_id] = _Quarantined(public_code, core_operation=current.core_operation)

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
            resumes = (
                *(
                    operation.task
                    for operation in self._operations.values()
                    if isinstance(operation, (_ResumeInFlight, _RawCloseInFlight, SessionRetirementOperation)) and operation.task is not None
                ),
                *self._forks,
            )
        if resumes:
            await asyncio.gather(
                *(asyncio.shield(task) for task in resumes), return_exceptions=True
            )
        async with self._lock:
            self._by_session.clear()
            self._by_host.clear()
            core_operations = tuple(
                operation.core_operation for operation in self._operations.values()
                if isinstance(operation, (_ResumeInFlight, _RawCloseInFlight, _RuntimeReopenInFlight, _Quarantined))
                and operation.core_operation is not None
            )
            self._operations.clear()
        for core_operation in core_operations:
            await self.core.release_session_operation(core_operation)
        # Core owns execution shutdown, including Hosts opened by scheduling.
        # These maps only publish desktop handles.

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
            "title": summary.title or f"会话 {_display_session_id(summary.session_id)}",
            "subtitle": f"{summary.latest_entry_sequence} 条记录",
            "lifecycle": summary.lifecycle,
            "latest_root_turn": summary.latest_root_turn,
            "subagent_count": summary.subagent_count,
            "updated_at": updated_value,
            "latest_entry_sequence": summary.latest_entry_sequence,
            "model_call_binding": model_call_binding_to_dict(
                summary.model_call_binding
            ),
            "writer_generation": summary.writer_generation,
            "live": live,
            "workspace": {
                "id": summary.workspace_id,
                "name": summary.workspace_label,
                "path": summary.workspace_root,
                "kind": (
                    "quick" if summary.workspace_kind == "transient" else "project"
                ),
            },
        }


__all__ = [
    "HostSessionHandle",
    "LocalSessionController",
    "SessionWorkspaceKind",
]
