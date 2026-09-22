"""Cold-safe Session control plane for the local Web application."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime
import json
import logging
from pathlib import Path
import shlex
import sys
from typing import Literal, Mapping
from uuid import uuid4
from pulsara_agent.conversation_kernel.session_deletion import (
    KernelSessionDeletion, SessionDeleteRejected,
)
from pulsara_agent.conversation_kernel._repository.deletion import SessionDeletionBusy

from pulsara_agent.capability.local_skill_management import (
    InstallLooseLocalSkillRequest,
    LocalSkillInstallOutcome,
    LocalSkillInstallScope,
    LocalSkillManagementService,
    ValidateLocalSkillSourceRequest,
)
from pulsara_agent.capability.skill_import import enumerate_skill_import_sources
from pulsara_agent.capability.mcp_import import read_mcp_import, materialize_mcp_import
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
    workspace_skill_config_path,
)
from pulsara_agent.capability.workspace_mcp_trust import (
    workspace_mcp_server_approvals,
)
from pulsara_agent.capability.local_skill_removal import (
    LocalSkillRemovalIdentity,
    LocalSkillRemovalDisposition,
)
from pulsara_agent.conversation_kernel.host import (
    KernelCapabilityCatalogInspection,
    KernelHostCore,
    KernelHostCoreClosing,
    KernelHostSession,
    KernelSessionSummary,
    KernelRuntimeReopenQuiescence,
)
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    model_call_binding_to_dict,
)
from pulsara_agent.mcp_config import (
    default_user_mcp_config_path,
    McpLocalConfigSourceKind,
    McpScopePolicy,
    McpServerConfig,
    StdioTransportConfig,
    StreamableHttpTransportConfig,
    LegacySseTransportConfig,
    mcp_server_workspace_approval_identity,
)
from pulsara_agent.capability.mcp_management import (
    LocalMcpTarget,
    McpSecretMutation,
    config_guard,
    config_to_entry,
    credential_presence,
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


_NO_LIVE_HOST_SEAL = object()
_RUNTIME_REOPEN_OPERATION_SEAL = object()
_RUNTIME_REOPEN_OUTCOME_SEAL = object()
_RAW_CLOSE_OPERATION_SEAL = object()


class SessionControlRejected(RuntimeError):
    """Typed process-local rejection for a current Session control fence."""

    def __init__(self, public_code: str, message: str) -> None:
        super().__init__(message)
        self.public_code = public_code


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
    close_conversation: bool
    operation_nonce: str
    _owner: "LocalSessionController"

    def __init__(
        self,
        *,
        session_id: str,
        handle: HostSessionHandle,
        close_conversation: bool,
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
        object.__setattr__(self, "close_conversation", close_conversation)
        object.__setattr__(self, "operation_nonce", operation_nonce)
        object.__setattr__(self, "_owner", owner)


@dataclass(frozen=True, slots=True, init=False)
class OldHostCloseFull:
    operation: PreparedRuntimeReopenOperation

    def __init__(self, operation: PreparedRuntimeReopenOperation, *, _seal: object):
        if _seal is not _RUNTIME_REOPEN_OUTCOME_SEAL:
            raise TypeError("old-Host close outcome is controller-issued")
        object.__setattr__(self, "operation", operation)


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


@dataclass(slots=True)
class _RawCloseInFlight:
    operation: PreparedRawCloseOperation
    physical_close_full: bool = False


@dataclass(slots=True)
class _RuntimeReopenInFlight:
    operation: RuntimeReopenOperation
    abort_requested: bool = False


@dataclass(frozen=True, slots=True)
class _Quarantined:
    public_code: str


@dataclass(eq=False, slots=True)
class SessionDeleteOperation:
    session_id: str
    owner: "LocalSessionController"
    task: asyncio.Task | None = None
    prior_resume: asyncio.Task | None = None
    core_operation: KernelSessionDeletion | None = None
    detach: object | None = None
    unconfirmed: bool = False


_SessionOperation = (
    _ResumeInFlight | _RawCloseInFlight | _RuntimeReopenInFlight | _Quarantined | SessionDeleteOperation
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


class LocalSessionController:
    """Own create/resume publication while preserving Kernel close authority."""

    def __init__(
        self,
        *,
        core: KernelHostCore,
        workspace_input: HostWorkspaceInput,
        permission_policy: EffectivePermissionPolicy,
        active_skill_names: frozenset[str],
    ) -> None:
        self.core = core
        self.workspace_input = workspace_input
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
        batch_id: str | None = None,
    ) -> dict[str, object]:
        """Cold-read one session's complete durable task inventory by page."""

        if not 1 <= maximum_items <= _TASK_PAGE_MAXIMUM:
            raise ValueError("task page size is out of bounds")
        if batch_id is not None and not batch_id:
            raise ValueError("task batch id is invalid")
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
                batch_id=batch_id,
            )
        durable, dependency_rows = await self.core.read_subagent_task_page(
            session_id=session_id,
            maximum_items=maximum_items,
            after_accepted_at=after_accepted_at,
            after_task_id=after_task_id,
            batch_id=batch_id,
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
                batch_id=batch_id,
            )
        return {
            "session_id": session_id,
            "tasks": tasks,
            "total_count": total_count,
            "page_count": len(tasks),
            "remaining_count": max(0, total_count - next_seen_count),
            "next_cursor": next_cursor,
        }

    async def list_session_task_groups(
        self,
        session_id: str,
        *,
        maximum_items: int = _TASK_PAGE_MAXIMUM,
        cursor: str | None = None,
    ) -> dict[str, object]:
        """Cold-read one page of exact durable task batches."""

        if not 1 <= maximum_items <= _TASK_PAGE_MAXIMUM:
            raise ValueError("task group page size is out of bounds")
        summary = await self.core.read_resumable_session(
            session_id,
            memory_domain_id=self.workspace_input.memory_domain_id,
        )
        if summary is None:
            raise KeyError(session_id)
        after_at: datetime | None = None
        after_batch_id: str | None = None
        seen_count = 0
        if cursor is not None:
            after_at, after_batch_id, seen_count = _decode_task_group_cursor(
                cursor, session_id=session_id
            )
        durable = await self.core.read_subagent_task_group_page(
            session_id=session_id,
            maximum_items=maximum_items,
            after_first_accepted_at=after_at,
            after_batch_id=after_batch_id,
        )
        has_more = len(durable) > maximum_items
        page = durable[:maximum_items]
        total_count = int(page[0]["total_count"]) if page else seen_count
        next_seen_count = seen_count + len(page)
        groups = []
        for row in page:
            first = row["first_accepted_at"]
            if not isinstance(first, datetime):
                raise RuntimeError("task group lacks its ordering timestamp")
            groups.append(
                {
                    "group_id": str(row["batch_id"]),
                    "parent_turn_id": str(row["parent_turn_id"]),
                    "first_accepted_at": first.isoformat(),
                    "task_count": int(row["task_count"]),
                    "status_counts": {
                        "pending": int(row["pending_count"]),
                        "active": int(row["active_count"]),
                        "waiting": int(row["waiting_count"]),
                        "completed": int(row["completed_count"]),
                        "cancelled": int(row["cancelled_count"]),
                        "failed": int(row["failed_count"]),
                        "interrupted": int(row["interrupted_count"]),
                        "blocked": int(row["blocked_count"]),
                    },
                    "single_task_label": row.get("single_task_label"),
                }
            )
        next_cursor = None
        if has_more:
            last = page[-1]
            first = last["first_accepted_at"]
            if not isinstance(first, datetime):
                raise RuntimeError("task group lacks its ordering timestamp")
            next_cursor = _encode_task_group_cursor(
                session_id=session_id,
                first_accepted_at=first,
                batch_id=str(last["batch_id"]),
                seen_count=next_seen_count,
            )
        return {
            "session_id": session_id,
            "groups": groups,
            "total_count": total_count,
            "page_count": len(groups),
            "remaining_count": max(0, total_count - next_seen_count),
            "next_cursor": next_cursor,
        }

    async def list_session_task_activities(
        self,
        session_id: str,
        task_id: str,
        *,
        maximum_items: int = _TASK_PAGE_MAXIMUM,
        cursor: str | None = None,
    ) -> dict[str, object]:
        """Read canonical activity identities without activating a Host."""

        if not 1 <= maximum_items <= _TASK_PAGE_MAXIMUM:
            raise ValueError("task activity page size is out of bounds")
        summary = await self.core.read_resumable_session(
            session_id,
            memory_domain_id=self.workspace_input.memory_domain_id,
        )
        if summary is None:
            raise KeyError(session_id)
        after_sequence = 0
        if cursor is not None:
            after_sequence = _decode_task_activity_cursor(
                cursor, session_id=session_id, task_id=task_id
            )
        (
            entries,
            blocks,
            results,
            has_more,
        ) = await self.core.read_subagent_task_activity_page(
            session_id=session_id,
            task_id=task_id,
            maximum_items=maximum_items,
            after_entry_sequence=after_sequence,
        )
        blocks_by_entry: dict[str, list[dict[str, object]]] = {}
        for block in blocks:
            blocks_by_entry.setdefault(str(block["assistant_entry_id"]), []).append(
                {
                    "block_id": str(block["id"]),
                    "ordinal": int(block["block_ordinal"]),
                    "kind": str(block["block_kind"]),
                    "tool_call_id": block.get("tool_call_id"),
                    "tool_name": block.get("tool_name"),
                }
            )
        results_by_entry: dict[str, list[dict[str, object]]] = {}
        for result in results:
            result_payload = {
                "attempt_id": result.get("attempt_id"),
                "assistant_entry_id": str(result["assistant_entry_id"]),
                "tool_call_id": str(result["tool_call_id"]),
                "result_entry_id": str(result["result_entry_id"]),
                "result_state": str(result["result_state"]),
            }
            results_by_entry.setdefault(str(result["assistant_entry_id"]), []).append(
                result_payload
            )
            results_by_entry.setdefault(str(result["result_entry_id"]), []).append(
                result_payload
            )
        activities = []
        for entry in entries:
            inline = entry.get("inline_content")
            content = {
                "kind": "INLINE" if inline is not None else "CANONICAL_BLOB",
                "digest": str(entry["content_digest"]),
                "size": int(entry["content_size"]),
                "media_type": str(entry["content_media_type"]),
                "codec": str(entry["content_codec"]),
            }
            if inline is not None:
                content["inline_content"] = base64.b64encode(bytes(inline)).decode(
                    "ascii"
                )
            entry_id = str(entry["id"])
            activities.append(
                {
                    "entry_id": entry_id,
                    "turn_id": str(entry["turn_id"]),
                    "entry_sequence": int(entry["entry_sequence"]),
                    "entry_kind": str(entry["entry_kind"]),
                    "accepted_at": entry["accepted_at"].isoformat(),
                    "objective": str(entry["task_objective"]),
                    "content": content,
                    "blocks": blocks_by_entry.get(entry_id, []),
                    "tool_results": results_by_entry.get(entry_id, []),
                }
            )
        next_cursor = None
        if has_more and activities:
            next_cursor = _encode_task_activity_cursor(
                session_id=session_id,
                task_id=task_id,
                after_entry_sequence=int(activities[-1]["entry_sequence"]),
            )
        return {
            "session_id": session_id,
            "task_id": task_id,
            "activities": activities,
            "next_cursor": next_cursor,
        }

    async def inspect_session_capabilities(self, session_id: str) -> dict[str, object]:
        """Project current capability owners for one selected Session."""

        handle = await self.resume_session(session_id)
        return await self._session_capability_payload(handle)

    async def reconnect_session_mcp(
        self, session_id: str, server_id: str
    ) -> dict[str, object]:
        if not server_id:
            raise ValueError("MCP server id is required")
        handle = await self.resume_session(session_id)
        handle.session.reconnect_mcp_server(server_id)
        return await self._session_capability_payload(handle)

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
        async with self._capability_mutation_lock:
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
            if outcome.disposition.value == "INSTALLED"
            else 0
        )
        capabilities = await self._session_capability_payload(handle)
        return {
            "installation": _skill_install_payload(outcome),
            "adoption": _workspace_adoption_payload(pending),
            "capabilities": capabilities,
        }

    async def set_session_skill_enabled(
        self,
        session_id: str,
        *,
        skill_id: str,
        enabled: bool,
    ) -> dict[str, object]:
        handle = await self.resume_session(session_id)
        workspace_root = handle.session.workspace.workspace_root
        async with self._capability_mutation_lock:
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

    async def remove_session_skill(self, session_id, *, skill_path, expected):
        handle = await self.resume_session(session_id)
        workspace_root = handle.session.workspace.workspace_root
        async with self._capability_mutation_lock:
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
        return await self._workspace_mcp_settlement(handle, outcome, "ADDED")

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
        return await self._workspace_mcp_settlement(handle, result, "UPDATED")

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
            handle, outcome, "ENABLED" if enabled else "DISABLED"
        )

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
        return await self._workspace_mcp_settlement(handle, outcome, "REMOVED")

    async def _workspace_mcp_settlement(self, handle, outcome, status):
        pending = await self._mark_capability_refresh(
            handle.session.workspace.workspace_root
        )
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

    async def inspect_user_capabilities(
        self, *, active_session_id: str | None = None
    ) -> dict[str, object]:
        """Read the user-owned Skill, MCP, and Plugin inventory."""

        workspace_root = resolve_workspace(self.workspace_input).workspace_root
        skills_task = asyncio.to_thread(_inspect_user_skills, workspace_root)
        mcp_task = asyncio.to_thread(self.core.mcp_management.load_configs)
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
                secret_resolver=self.core.mcp_management.settings.read().mcp_secret,
                live_inspection=live_inspection,
            )
        finally:
            if isinstance(plugins, PluginInspectionOutcome):
                plugins.close()

    async def refresh_user_capabilities(
        self, *, active_session_id: str | None = None
    ) -> dict[str, object]:
        """Adopt current user sources in every live Session, then inspect them."""

        updated, attention = await self._reload_live_capabilities()
        payload = await self.inspect_user_capabilities(
            active_session_id=active_session_id
        )
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

    async def install_user_skill(
        self,
        *,
        source_path: str,
        active_session_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, object]:
        source = _absolute_local_source(source_path, "Skill")
        async with self._capability_mutation_lock:
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
            if outcome.disposition.value == "INSTALLED"
            else 0
        )
        capabilities = await self.inspect_user_capabilities(
            active_session_id=active_session_id
        )
        return {
            "operation": _skill_install_payload(outcome),
            "adoption": {
                "updated_sessions": 0,
                "pending_sessions": pending,
                "attention_sessions": 0,
            },
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
        pending = await self._mark_capability_refresh()
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

    async def remove_user_skill(
        self,
        *,
        skill_path: str,
        expected: LocalSkillRemovalIdentity,
        active_session_id: str | None = None,
    ) -> dict[str, object]:
        path = Path(skill_path).expanduser()
        service = LocalSkillManagementService()
        async with self._capability_mutation_lock:
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
            "capabilities": await self.inspect_user_capabilities(
                active_session_id=active_session_id
            ),
        }

    async def create_user_mcp_server(
        self,
        *,
        server_id: str,
        config: dict[str, object],
        secret_changes: tuple[McpSecretMutation, ...] = (),
        active_session_id: str | None = None,
    ) -> dict[str, object]:
        outcome = await self.core.mcp_management.create(
            LocalMcpTarget(server_id),
            config,
            secret_changes,
        )
        return await self._user_mcp_settlement(outcome, "ADDED", active_session_id)

    async def preview_mcp_import(
        self, *, content: str, shape: str, server_id: str | None
    ):
        drafts = await asyncio.to_thread(
            read_mcp_import, content, shape=shape, server_id=server_id
        )
        return {"items": [draft.preview() for draft in drafts]}

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
        active_session_id: str | None = None,
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
            return await self._workspace_mcp_settlement(handle, outcome, "ADDED")
        return await self._user_mcp_settlement(outcome, "ADDED", active_session_id)

    async def update_user_mcp_server(
        self,
        *,
        server_id: str,
        config: dict[str, object],
        expected: str,
        secret_changes: tuple[McpSecretMutation, ...] = (),
        active_session_id: str | None = None,
        retain_credentials_confirmed: bool = False,
    ) -> dict[str, object]:
        outcome = await self.core.mcp_management.update(
            LocalMcpTarget(server_id),
            config,
            expected=expected,
            secrets=secret_changes,
            retain_credentials_confirmed=retain_credentials_confirmed,
        )
        return await self._user_mcp_settlement(outcome, "UPDATED", active_session_id)

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
            else resolve_workspace(self.workspace_input).workspace_root
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

    async def remove_user_mcp_server(
        self,
        *,
        server_id: str,
        expected: str,
        active_session_id: str | None = None,
    ) -> dict[str, object]:
        outcome = await self.core.mcp_management.remove(
            LocalMcpTarget(server_id), expected=expected
        )
        return await self._user_mcp_settlement(outcome, "REMOVED", active_session_id)

    async def _user_mcp_settlement(self, outcome, status, active_session_id):
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
            "capabilities": await self.inspect_user_capabilities(
                active_session_id=active_session_id
            ),
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
        handle = (
            await self.resume_session(session_id) if session_id is not None else None
        )
        owner = LocalMcpTarget(
            server_id, handle.session.workspace.workspace_root if handle else None
        ).owner
        manager = self.core.mcp_management.oauth
        if action == "cancel":
            await manager.cancel(owner)
        elif action == "logout":
            await manager.logout(owner)
        elif action != "status":
            raise ValueError("invalid MCP authorization action")
        return manager.login_state(owner)

    async def install_user_plugin(
        self,
        *,
        source_path: str,
        active_session_id: str | None = None,
        source_format="native",
        classifications=(),
        public_values=(),
    ) -> dict[str, object]:
        source = _absolute_local_source(source_path, "Plugin")
        outcome = await self.core.install_user_plugin(
            source,
            source_format=source_format,
            import_classifications=classifications,
            import_public_values=public_values,
        )
        capabilities = await self.inspect_user_capabilities(
            active_session_id=active_session_id
        )
        return {
            "operation": _plugin_install_operation(outcome),
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

    async def set_user_plugin_enabled(
        self,
        *,
        plugin_id: str,
        package_install_id: str,
        enabled: bool,
        connection_review,
        active_session_id: str | None = None,
    ) -> dict[str, object]:
        async with self._capability_mutation_lock:
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
        capabilities = await self.inspect_user_capabilities(
            active_session_id=active_session_id
        )
        return {
            "operation": _plugin_enablement_operation(outcome),
            "adoption": {
                "updated_sessions": 0,
                "pending_sessions": pending,
                "attention_sessions": 0,
            },
            "capabilities": capabilities,
        }

    async def remove_user_plugin(
        self,
        *,
        plugin_id: str,
        package_install_id: str,
        active_session_id: str | None = None,
    ) -> dict[str, object]:
        outcome = await self.core.remove_user_plugin(plugin_id, package_install_id)
        pending = (
            await self._mark_capability_refresh()
            if isinstance(outcome, RemovedPluginOutcome)
            else 0
        )
        capabilities = await self.inspect_user_capabilities(
            active_session_id=active_session_id
        )
        return {
            "operation": _plugin_removal_operation(outcome),
            "adoption": {
                "updated_sessions": 0,
                "pending_sessions": pending,
                "attention_sessions": 0,
            },
            "capabilities": capabilities,
        }

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
        active_session_id=None,
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
            "capabilities": await self.inspect_user_capabilities(
                active_session_id=active_session_id
            ),
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
        """Web and model mutations share the Host's adoption owner."""
        return await self.core.mark_capability_change(workspace_root)

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

    async def read_session(self, session_id: str) -> dict[str, object] | None:
        summary = await self.core.read_resumable_session(
            session_id,
            memory_domain_id=self.workspace_input.memory_domain_id,
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
                raise KernelHostCoreClosing("Local Web application is draining")
            if source_session_id in self._operations:
                raise SessionControlRejected("SESSION_CONTROL_BUSY", "会话正在处理另一项操作。")
            child_session_id = f"session:{uuid4().hex}"
            task = asyncio.create_task(
                self._fork_owner(source_session_id, anchor_entry_id, child_session_id),
                name=f"local-web-fork:{child_session_id}",
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
        creation = await self.core.fork_conversation(
            source_session_id=source_session_id,
            anchor_entry_id=anchor_entry_id,
            child_session_id=child_session_id,
            memory_domain_id=self.workspace_input.memory_domain_id,
        )
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
                memory_domain_id=self.workspace_input.memory_domain_id,
            )
            if summary is None:
                raise KeyError(session_id)
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Local Web application is draining")
            live = self._by_session.get(session_id)
            current = self._operations.get(session_id)
            if isinstance(current, _Quarantined):
                raise SessionControlRejected(
                    current.public_code,
                    "Session runtime is quarantined; restart Pulsara to continue.",
                )
            if live is not None:
                if no_live_observation is not None or current is not None:
                    raise RuntimeError("Session has another current operation")
                return live
            if isinstance(current, _ResumeInFlight):
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
                task = asyncio.create_task(
                    self._resume_owner(session_id),
                    name=f"local-web-resume:{session_id}",
                )
                self._operations[session_id] = _ResumeInFlight(task)
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
                display_label=(
                    None
                    if summary.workspace_kind == "project"
                    else summary.workspace_label
                ),
                memory_domain_id=summary.memory_domain_id,
                cleanup_workspace_root_on_close=False,
                trust_workspace_mcp_config=(
                    self.workspace_input.trust_workspace_mcp_config
                ),
            )
            session = await self.core.resume_session(
                session_id,
                workspace_input=workspace_input,
                permission_policy=self.permission_policy,
                active_skill_names=self.active_skill_names,
            )
            handle = HostSessionHandle(session, workspace_input)
            try:
                raced: HostSessionHandle | None = None
                async with self._lock:
                    if self._closing:
                        raise KernelHostCoreClosing("Local Web application is draining")
                    current = self._operations.get(session_id)
                    deleting = isinstance(current, SessionDeleteOperation) and current.prior_resume is task
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
                current = self._operations.get(session_id)
                if isinstance(current, _ResumeInFlight) and current.task is task:
                    self._operations.pop(session_id, None)

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
            operation_nonce=f"no-live-host:{uuid4().hex}",
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

    async def update_model_call_binding(
        self, session_id: str, binding: ModelCallBinding
    ) -> dict[str, object]:
        handle = await self.resume_session(session_id)
        accepted = await handle.session.update_model_call_binding(binding)
        return {
            "model_call_binding": model_call_binding_to_dict(accepted),
            "reasoning_preference_reset": accepted != binding,
        }

    async def delete_session(self, session_id: str, *, bridge) -> dict[str, object]:
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Local Web application is draining")
            current = self._operations.get(session_id)
            if isinstance(current, SessionDeleteOperation):
                operation = current
                if operation.unconfirmed and operation.task is not None and operation.task.done():
                    operation.task = asyncio.create_task(self._confirm_session_deletion(operation, bridge))
            else:
                if current is not None and not isinstance(current, _ResumeInFlight):
                    raise SessionDeleteRejected(
                        "SESSION_DELETE_QUARANTINED" if isinstance(current, _Quarantined) else "SESSION_DELETE_BUSY",
                        "会话尚不能删除，请等待当前操作结束；若已隔离，请重启 Pulsara。",
                    )
                operation = SessionDeleteOperation(
                    session_id, self, prior_resume=current.task if isinstance(current, _ResumeInFlight) else None,
                )
                self._operations[session_id] = operation
                operation.task = asyncio.create_task(
                    self._delete_session_owner(operation, bridge), name=f"session-delete:{session_id}",
                )
        return await asyncio.shield(operation.task)

    async def confirm_session_delete_operation(self, operation: SessionDeleteOperation) -> None:
        async with self._lock:
            if operation.owner is not self or self._operations.get(operation.session_id) is not operation:
                raise RuntimeError("session delete operation is stale")

    async def _finish_session_delete(self, operation, bridge, *, quarantine: bool) -> None:
        from pulsara_agent.web_app.browser_bridge import BridgeSettlementFailed
        if operation.detach is not None:
            try:
                result = await bridge.settle_session_delete_detach(operation.detach, close_full=not quarantine)
                quarantine = quarantine or isinstance(result, BridgeSettlementFailed)
            except Exception:
                logging.getLogger(__name__).exception("session deletion bridge cleanup failed")
                quarantine = True
            operation.detach = None
        if operation.core_operation is not None:
            try:
                await self.core.finish_session_deletion(operation.core_operation, quarantine=quarantine)
            except Exception:
                logging.getLogger(__name__).exception("session deletion core cleanup failed")
                quarantine = True
        async with self._lock:
            if self._operations.get(operation.session_id) is operation:
                if quarantine:
                    self._operations[operation.session_id] = _Quarantined("SESSION_DELETE_QUARANTINED")
                else:
                    self._operations.pop(operation.session_id)

    async def _confirm_session_deletion(self, operation, bridge) -> dict[str, object]:
        try:
            exists = await self.core.canonical_session_exists(
                operation.session_id, self.workspace_input.memory_domain_id,
            )
        except Exception as exc:
            operation.unconfirmed = True
            if operation.core_operation is not None:
                await self.core.finish_session_deletion(operation.core_operation, quarantine=True)
            raise SessionDeleteRejected(
                "SESSION_DELETE_UNCONFIRMED", "暂时无法确认删除结果，请恢复数据库连接后重试确认。", 503,
            ) from exc
        operation.unconfirmed = False
        await self._finish_session_delete(operation, bridge, quarantine=False)
        if exists:
            raise SessionDeleteRejected("SESSION_DELETE_FAILED", "会话仍在，尚未删除，可以重试。", 500)
        return {"status": "ABSENT", "session_id": operation.session_id}

    async def _delete_session_owner(self, operation, bridge) -> dict[str, object]:
        from pulsara_agent.web_app.browser_bridge import BridgeDetachFull, BridgeDetachFailed
        committed = False
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
            exists = await self.core.canonical_session_exists(
                operation.session_id, self.workspace_input.memory_domain_id,
            )
            async with self._lock:
                handle = self._by_session.get(operation.session_id)
            if not exists and handle is None:
                await self._finish_session_delete(operation, bridge, quarantine=False)
                return {"status": "ABSENT", "session_id": operation.session_id}
            if handle is not None and handle.workspace_input.memory_domain_id != self.workspace_input.memory_domain_id:
                raise RuntimeError("session delete domain mismatch")
            operation.core_operation = await self.core.begin_session_deletion(
                operation.session_id, self.workspace_input.memory_domain_id,
            )
            detach = await bridge.detach_session_for_delete(operation)
            if isinstance(detach, (BridgeDetachFull, BridgeDetachFailed)):
                operation.detach = detach.token
            if not isinstance(detach, BridgeDetachFull):
                raise SessionDeleteRejected("SESSION_DELETE_QUARANTINED", "未能安全断开会话，会话尚未删除。")
            await self.core.quiesce_session_deletion(operation.core_operation)
            async with self._lock:
                handle = self._by_session.pop(operation.session_id, None)
                if handle is not None:
                    self._by_host.pop(handle.host_session_id, None)
            try:
                status = await self.core.commit_session_deletion(operation.core_operation)
            except SessionDeletionBusy as exc:
                raise SessionDeleteRejected("SESSION_DELETE_BUSY", "会话的运行权已改变，或仍被另一进程使用，请稍后重试。") from exc
            except Exception:
                logging.getLogger(__name__).exception("session deletion commit needs confirmation")
                return await self._confirm_session_deletion(operation, bridge)
            committed = True
            await self._finish_session_delete(operation, bridge, quarantine=False)
            return {"status": status, "session_id": operation.session_id}
        except Exception as exc:
            if operation.unconfirmed:
                raise
            if self._operations.get(operation.session_id) is operation:
                quarantine = operation.core_operation is not None and not operation.core_operation.physical_full
                await self._finish_session_delete(operation, bridge, quarantine=quarantine)
            if committed:
                logging.getLogger(__name__).exception("session deleted; local cleanup needs refresh")
                return {"status": "DELETED", "session_id": operation.session_id}
            if isinstance(exc, SessionDeleteRejected):
                raise
            raise SessionDeleteRejected("SESSION_DELETE_QUARANTINED", "未能安全停止，会话尚未删除，请重启 Pulsara 后重试。") from exc

    async def prepare_raw_close(
        self, session_id: str, *, close_conversation: bool
    ) -> PreparedRawCloseOperation | None:
        if close_conversation:
            async with self._lock:
                needs_resume = self._by_session.get(session_id) is None
            if needs_resume:
                await self.resume_session(session_id)
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Local Web application is draining")
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
                close_conversation=close_conversation,
                operation_nonce=f"raw-close:{uuid4().hex}",
                owner=self,
                _seal=_RAW_CLOSE_OPERATION_SEAL,
            )
            self._operations[session_id] = _RawCloseInFlight(operation)
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
        try:
            await self.core.close_session(
                operation.handle.host_session_id,
                close_conversation=operation.close_conversation,
            )
        except BaseException:
            async with self._lock:
                current = self._operations.get(session_id)
                if (
                    isinstance(current, _RawCloseInFlight)
                    and current.operation is operation
                ):
                    self._operations[session_id] = _Quarantined(
                        "SESSION_CLOSE_QUARANTINED"
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
        from pulsara_agent.web_app.browser_bridge import BridgeSettlementFull

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

    async def quarantine_raw_close(
        self, operation: PreparedRawCloseOperation, *, public_code: str
    ) -> None:
        async with self._lock:
            current = self._operations.get(operation.session_id)
            if (
                isinstance(current, _RawCloseInFlight)
                and current.operation is operation
            ):
                self._operations[operation.session_id] = _Quarantined(public_code)

    async def prepare_runtime_reopen(self, session_id: str) -> RuntimeReopenOperation:
        if not session_id:
            raise ValueError("session_id is required")
        summary = await self.core.read_resumable_session(
            session_id,
            memory_domain_id=self.workspace_input.memory_domain_id,
        )
        if summary is None:
            raise KeyError(session_id)
        async with self._lock:
            if self._closing:
                raise KernelHostCoreClosing("Local Web application is draining")
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
            operation: RuntimeReopenOperation
            if handle is None:
                observation = self._issue_no_live_observation_locked(session_id)
                observation._consume(self)
                operation = PreparedRuntimeResumeOperation(
                    session_id=session_id,
                    operation_nonce=f"runtime-resume:{uuid4().hex}",
                    owner=self,
                    _seal=_RUNTIME_REOPEN_OPERATION_SEAL,
                )
            else:
                try:
                    quiescence = await handle.session.prepare_safe_runtime_reopen()
                except RuntimeError as exc:
                    raise SessionControlRejected(
                        "RUNTIME_REOPEN_BUSY",
                        "Session still has accepted process-local work.",
                    ) from exc
                operation = PreparedRuntimeReopenOperation(
                    session_id=session_id,
                    old_handle=handle,
                    operation_nonce=f"runtime-reopen:{uuid4().hex}",
                    host_quiescence=quiescence,
                    owner=self,
                    _seal=_RUNTIME_REOPEN_OPERATION_SEAL,
                )
            self._operations[session_id] = _RuntimeReopenInFlight(operation)
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
        from pulsara_agent.web_app.browser_bridge import BridgeSettlementFull

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
                close_conversation=False,
            )
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
    ) -> StableNoLiveHostObservation:
        from pulsara_agent.web_app.browser_bridge import BridgeSettlementFull

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
                self._operations[operation.session_id] = _Quarantined(public_code)

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
                    if isinstance(operation, (_ResumeInFlight, SessionDeleteOperation)) and operation.task is not None
                ),
                *self._forks,
            )
        if resumes:
            await asyncio.gather(
                *(asyncio.shield(task) for task in resumes), return_exceptions=True
            )
        async with self._lock:
            handles = tuple(self._by_session.values())
            self._by_session.clear()
            self._by_host.clear()
            self._operations.clear()
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
            "model_call_binding": model_call_binding_to_dict(
                summary.model_call_binding
            ),
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
_WORKSPACE_SKILL_ROOTS = frozenset(
    {
        LocalSkillRootKind.WORKSPACE_PULSARA,
        LocalSkillRootKind.WORKSPACE_AGENTS,
    }
)


def _workspace_adoption_payload(pending_sessions: int) -> dict[str, object]:
    return {
        "scope": "workspace",
        "pending_sessions": pending_sessions,
        "when": "next_provider_dispatch",
    }


def _absolute_local_source(value: str, label: str) -> Path:
    source = Path(value.strip()).expanduser()
    if not value.strip() or not source.is_absolute():
        raise ValueError(f"{label} source path must be absolute")
    return source


def _skill_removal_identity(
    path: Path, scope: LocalSkillInstallScope, workspace_root: Path | None = None
):
    try:
        identity = LocalSkillManagementService().inspect_loose_skill_removal(
            skill_path=path,
            scope=scope,
            workspace_root=workspace_root,
        )
    except (ValueError, OSError):
        return None
    return {
        name: str(getattr(identity, name))
        for name in (
            "root_device",
            "root_inode",
            "directory_device",
            "directory_inode",
        )
    }


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
        if (
            not isinstance(origin, LooseSkillOrigin)
            or origin.root_kind not in _USER_SKILL_ROOTS
        ):
            continue
        items.append(
            {
                "name": item.name,
                "description": item.description,
                "location": item.location,
                "path": str(item.path),
                "removal_identity": _skill_removal_identity(
                    item.path, LocalSkillInstallScope.USER
                ),
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
        if (
            not isinstance(origin, LooseSkillOrigin)
            or origin.root_kind not in _USER_SKILL_ROOTS
        ):
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


def _workspace_skill_root_key(root_kind: LocalSkillRootKind) -> str:
    if root_kind is LocalSkillRootKind.WORKSPACE_PULSARA:
        return "pulsara"
    if root_kind is LocalSkillRootKind.WORKSPACE_AGENTS:
        return "agents"
    raise ValueError("Skill is not in a project capability directory")


def _inspect_workspace_skills(workspace_root: Path) -> dict[str, object]:
    producer = LooseSkillDefinitionProducer()
    config = load_user_skill_config(
        config_path=workspace_skill_config_path(workspace_root)
    )
    policy = producer.prepare_root_policy(workspace_root)
    observed = producer.observe(policy)
    roots = [
        {
            "kind": _workspace_skill_root_key(item.root_kind),
            "path": str(item.path),
        }
        for item in policy.roots
        if item.root_kind in _WORKSPACE_SKILL_ROOTS
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
    items: list[dict[str, object]] = []
    for item in observed.candidates:
        origin = item.origin
        if (
            not isinstance(origin, LooseSkillOrigin)
            or origin.root_kind not in _WORKSPACE_SKILL_ROOTS
        ):
            continue
        root = _workspace_skill_root_key(origin.root_kind)
        items.append(
            {
                "id": f"{root}:{item.name}",
                "removal_identity": _skill_removal_identity(
                    item.path, LocalSkillInstallScope.WORKSPACE, workspace_root
                ),
                "name": item.name,
                "description": item.description,
                "location": item.location,
                "path": str(item.path),
                "enabled": config.enabled_for(item.path),
                "root": root,
                "authoring_notes": [
                    code.value for code in item.authoring_diagnostic_codes
                ],
            }
        )
    issues: list[dict[str, object]] = []
    for issue in observed.invalid_issues:
        origin = issue.origin
        if (
            not isinstance(origin, LooseSkillOrigin)
            or origin.root_kind not in _WORKSPACE_SKILL_ROOTS
        ):
            continue
        issues.append(
            {
                "kind": "invalid",
                "title": "有一个项目技能没有通过检查",
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


def _workspace_skill_path(observed: dict[str, object], skill_id: str) -> Path:
    raw_items = observed.get("items")
    if not isinstance(raw_items, list):
        raise RuntimeError("project Skill inventory is invalid")
    matches = [
        item
        for item in raw_items
        if isinstance(item, dict) and item.get("id") == skill_id
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("path"), str):
        raise ValueError("Skill is not in this project's capability directories")
    return Path(str(matches[0]["path"])).resolve(strict=False)


def _user_capability_payload(
    *,
    skills: dict[str, object],
    mcp_configs: tuple[McpServerConfig, ...],
    plugins: object,
    secret_resolver,
    live_inspection: KernelCapabilityCatalogInspection | None,
) -> dict[str, object]:
    live_catalog = (
        {item.server_id: item for item in live_inspection.mcp_catalog.servers}
        if live_inspection is not None
        else {}
    )
    live_tools: dict[str, list[dict[str, object]]] = {}
    live_configs = (
        {item.server_id: item for item in live_inspection.mcp_configured_servers}
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
            and live_config.resolved_config_identity == config.resolved_config_identity
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
        elif isinstance(
            config.transport, (StreamableHttpTransportConfig, LegacySseTransportConfig)
        ):
            transport = {
                "kind": "http",
                "summary": config.transport.endpoint,
            }
        else:  # pragma: no cover - closed transport union
            raise TypeError("MCP transport is open")
        mcp_servers.append(
            {
                "id": config.server_id,
                "config": config_to_entry(config),
                "current_identity": config_guard(config),
                "credentials": credential_presence(config),
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
                "failure_category": (
                    live.stable_failure_category if live is not None else None
                ),
                "transport": transport,
                "tools": (
                    live_tools.get(config.server_id, []) if live is not None else []
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
            "config_path": str(default_user_mcp_config_path()),
            "servers": mcp_servers,
        },
        "plugins": _user_plugins_payload(plugins, secret_resolver),
    }


def _user_plugins_payload(inspection: object, secret_resolver) -> dict[str, object]:
    from pulsara_agent.plugins.mcp_connection import connection_review, review_to_dict

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
                "mcp_connections": _plugin_connection_editors(item),
                "connection_review": review_to_dict(
                    connection_review(
                        item.mcp_connection_overlays,
                        secret_resolver,
                        servers=item.summary.mcp.mcp_servers,
                        identity=item.identity,
                    )
                ),
                "details": [_diagnostic_message(value) for value in item.diagnostics],
            }
        )
    return {
        "status": "ready",
        "items": sorted(items, key=lambda item: str(item["name"])),
        "details": [_diagnostic_message(item) for item in inspection.diagnostics],
    }


def _plugin_connection_editors(instance):
    from pulsara_agent.plugins.mcp_connection import (
        connection_editor_definition,
        overlay_to_dict,
        plugin_connection_owner,
    )
    from dataclasses import asdict

    result = []
    for server in instance.summary.mcp.mcp_servers:
        overlay = next(
            (
                item
                for item in instance.mcp_connection_overlays
                if item.local_server_id == server.local_server_id
            ),
            None,
        )
        defaults, effective = connection_editor_definition(
            server,
            overlay,
            owner=plugin_connection_owner(instance.identity, server.local_server_id),
        )
        result.append(
            {
                "server_id": server.local_server_id,
                "defaults": defaults,
                "config": effective,
                "connection_inputs": [
                    asdict(value) for value in server.connection_inputs.inputs
                ],
                "overlay": overlay_to_dict(overlay) if overlay is not None else None,
                "credential_owner": asdict(
                    plugin_connection_owner(instance.identity, server.local_server_id)
                ),
            }
        )
    return result


def _diagnostic_message(value: object) -> str:
    message = getattr(value, "message", None)
    return (
        str(message) if isinstance(message, str) and message else type(value).__name__
    )


def _plugin_install_operation(outcome: object) -> dict[str, object]:
    if isinstance(outcome, CleanupUnavailablePluginInstallOutcome):
        result = _plugin_install_operation(outcome.prior)
        result["status"] = outcome.disposition.value
        result["message"] = f"{result['message']} 旧安装文件稍后可以清理。"
        return result
    if isinstance(
        outcome, (SuccessfulPluginInstallOutcome, AlreadyPresentPluginInstallOutcome)
    ):
        installed = isinstance(outcome, SuccessfulPluginInstallOutcome)
        return {
            "status": outcome.disposition.value,
            "success": True,
            "message": (
                "插件已安装；部分旧连接凭据清理需要检查。"
                if installed and outcome.cleanup_attention
                else "插件已经安装；开启后会在安全时机用于会话。"
                if installed
                else "这个插件已经安装。"
            ),
            "plugin_id": outcome.identity.plugin_id,
            "cleanup_attention": installed and outcome.cleanup_attention,
            "details": [_diagnostic_message(item) for item in outcome.diagnostics]
            if installed
            else [],
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
            "message": "插件已移除；部分本机凭据清理需要检查。"
            if outcome.cleanup_attention
            else "插件已移除。",
            "cleanup_attention": outcome.cleanup_attention,
        }
    if isinstance(outcome, FailedPluginRemovalOutcome):
        return {
            "status": outcome.disposition.value,
            "success": False,
            "message": "插件没有移除，请刷新后重试。",
            "details": [_diagnostic_message(item) for item in outcome.diagnostics],
        }
    raise TypeError("Plugin removal outcome is open")


def _mcp_transport_payload(config: McpServerConfig) -> dict[str, str]:
    if isinstance(config.transport, StdioTransportConfig):
        return {
            "kind": "stdio",
            "summary": config.transport.command,
            "detail": shlex.join((config.transport.command, *config.transport.args)),
        }
    if isinstance(
        config.transport, (StreamableHttpTransportConfig, LegacySseTransportConfig)
    ):
        return {
            "kind": "http",
            "summary": config.transport.endpoint,
            "detail": config.transport.endpoint,
        }
    raise TypeError("MCP transport is open")


def _mcp_source_label(source_kind: str | None) -> str:
    return {
        McpLocalConfigSourceKind.WORKSPACE.value: "workspace",
        McpLocalConfigSourceKind.USER.value: "user",
        McpLocalConfigSourceKind.HOST_OVERRIDE.value: "host",
        "MANAGED_PACKAGE": "plugin",
    }.get(source_kind, "host")


def _capability_payload(
    *,
    session_id: str,
    workspace_root: Path,
    workspace_kind: SessionWorkspaceKind,
    workspace_skills: dict[str, object],
    workspace_mcp_configs: tuple[McpServerConfig, ...],
    trust_all_workspace_mcp: bool,
    workspace_mcp_approvals: Mapping[str, str],
    refresh_pending: bool,
    refresh_attention: str | None,
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
    catalog_by_id = {item.server_id: item for item in catalog.servers}
    configured_by_id = {
        item.server_id: item for item in inspection.mcp_configured_servers
    }
    mcp_servers: list[dict[str, object]] = []
    workspace_mcp_ids = {item.server_id for item in workspace_mcp_configs}
    for config in workspace_mcp_configs:
        configured = configured_by_id.get(config.server_id)
        exact_workspace = (
            configured is not None
            and configured.source_kind == McpLocalConfigSourceKind.WORKSPACE.value
            and configured.resolved_config_identity == config.resolved_config_identity
        )
        live = catalog_by_id.get(config.server_id) if exact_workspace else None
        entry_trusted = trust_all_workspace_mcp or (
            workspace_mcp_approvals.get(config.server_id)
            == mcp_server_workspace_approval_identity(config)
        )
        enabled = config.enabled and entry_trusted
        status = (
            "DISABLED"
            if not config.enabled
            else "CONFIGURED"
            if not entry_trusted or live is None
            else live.status.value
        )
        mcp_servers.append(
            {
                "id": config.server_id,
                "name": config.display_name,
                "source": "workspace",
                "editable": True,
                "config_identity": mcp_server_workspace_approval_identity(config),
                "config": config_to_entry(config),
                "credentials": credential_presence(config),
                "enabled": enabled,
                "configured_enabled": config.enabled,
                "needs_approval": config.enabled and not entry_trusted,
                "effective": exact_workspace,
                "status": status,
                "required": config.required,
                "available_to_subagents": (
                    config.scope_policy is McpScopePolicy.ROOT_AND_SUBAGENTS
                ),
                "tool_count": live.exposed_tool_count if live is not None else 0,
                "discovered_tool_count": (
                    live.discovered_tool_count if live is not None else 0
                ),
                "resource_count": live.resource_count if live is not None else 0,
                "resource_template_count": (
                    live.resource_template_count if live is not None else 0
                ),
                "prompt_count": live.prompt_count if live is not None else 0,
                "instructions": (
                    live.sanitized_instructions if live is not None else ""
                ),
                "has_failure": (
                    live is not None and live.stable_failure_category is not None
                ),
                "failure_category": (
                    live.stable_failure_category if live is not None else None
                ),
                "transport": _mcp_transport_payload(config),
                "tools": tools_by_server.get(config.server_id, []) if live else [],
            }
        )
    for live in catalog.servers:
        configured = configured_by_id.get(live.server_id)
        if (
            live.server_id in workspace_mcp_ids
            and configured is not None
            and configured.source_kind == McpLocalConfigSourceKind.WORKSPACE.value
        ):
            continue
        source = _mcp_source_label(
            configured.source_kind if configured is not None else None
        )
        mcp_servers.append(
            {
                "id": live.server_id,
                "name": live.display_name,
                "source": source,
                "editable": False,
                "config_identity": None,
                "enabled": True,
                "configured_enabled": True,
                "needs_approval": False,
                "effective": True,
                "status": live.status.value,
                "required": live.required,
                "available_to_subagents": live.scope_subagents,
                "tool_count": live.exposed_tool_count,
                "discovered_tool_count": live.discovered_tool_count,
                "resource_count": live.resource_count,
                "resource_template_count": live.resource_template_count,
                "prompt_count": live.prompt_count,
                "instructions": live.sanitized_instructions,
                "has_failure": live.stable_failure_category is not None,
                "failure_category": live.stable_failure_category,
                "transport": None,
                "tools": tools_by_server.get(live.server_id, []),
            }
        )

    skill_catalog = inspection.skill_catalog
    skill_items: list[dict[str, object]] = []
    skill_issues: list[dict[str, object]] = []
    unavailable_details: list[str] = []
    if isinstance(skill_catalog, CompleteEffectiveSkillCatalogInspection):
        winner_paths = {
            item.path.resolve(strict=False): item for item in skill_catalog.winners
        }
        workspace_paths: set[Path] = set()
        raw_workspace_items = workspace_skills.get("items", [])
        if not isinstance(raw_workspace_items, list):
            raw_workspace_items = []
        for raw in raw_workspace_items:
            if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
                continue
            path = Path(str(raw["path"])).resolve(strict=False)
            workspace_paths.add(path)
            skill_items.append(
                {
                    "id": str(raw.get("id", "")),
                    "name": str(raw.get("name", "")),
                    "description": str(raw.get("description", "")),
                    "location": str(raw.get("location", "")),
                    "path": str(path),
                    "source": "workspace",
                    "editable": True,
                    "removal_identity": raw.get("removal_identity"),
                    "enabled": bool(raw.get("enabled", True)),
                    "effective": path in winner_paths,
                    "configured": str(raw.get("name", ""))
                    in inspection.configured_active_skill_names,
                    "authoring_notes": list(raw.get("authoring_notes", [])),
                }
            )
        for item in skill_catalog.winners:
            path = item.path.resolve(strict=False)
            if path in workspace_paths:
                continue
            skill_items.append(
                {
                    "id": f"{item.source.value}:{item.name}",
                    "name": item.name,
                    "description": item.description,
                    "location": item.location,
                    "path": str(path),
                    "source": item.source.value,
                    "editable": False,
                    "enabled": True,
                    "effective": True,
                    "configured": (
                        item.name in inspection.configured_active_skill_names
                    ),
                    "authoring_notes": [
                        code.value for code in item.authoring_diagnostic_codes
                    ],
                }
            )
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
        "workspace_kind": workspace_kind,
        "credential_scope_key": LocalMcpTarget("draft", workspace_root).owner.scope_key,
        "adoption": {
            "scope": "workspace",
            "pending": refresh_pending,
            "attention": refresh_attention,
            "when": "next_provider_dispatch",
        },
        "skills": {
            "status": (
                "ready"
                if isinstance(skill_catalog, CompleteEffectiveSkillCatalogInspection)
                else "attention"
            ),
            "items": skill_items,
            "issues": [
                *skill_issues,
                *(
                    workspace_skills.get("issues", [])
                    if isinstance(workspace_skills.get("issues"), list)
                    else []
                ),
            ],
            "details": [
                *unavailable_details,
                *(
                    workspace_skills.get("details", [])
                    if isinstance(workspace_skills.get("details"), list)
                    else []
                ),
            ],
            "config_path": str(
                workspace_skills.get(
                    "config_path", workspace_skill_config_path(workspace_root)
                )
            ),
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
            "config_path": str(
                workspace_root.resolve(strict=False) / ".pulsara/mcp.yaml"
            ),
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
        "completion_accepted": task.get("accepted_root_entry_id") is not None,
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
    batch_id: str | None,
) -> str:
    body = json.dumps(
        {
            "v": 1,
            "kind": "tasks",
            "session_id": session_id,
            "batch_id": batch_id,
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
    batch_id: str | None,
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
        "kind",
        "session_id",
        "batch_id",
        "accepted_at",
        "task_id",
        "seen_count",
    }:
        raise ValueError("task cursor is invalid")
    if (
        value["v"] != 1
        or value["kind"] != "tasks"
        or value["session_id"] != session_id
        or value["batch_id"] != batch_id
    ):
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


def _encode_task_group_cursor(
    *,
    session_id: str,
    first_accepted_at: datetime,
    batch_id: str,
    seen_count: int,
) -> str:
    body = json.dumps(
        {
            "v": 1,
            "kind": "task-groups",
            "session_id": session_id,
            "first_accepted_at": first_accepted_at.isoformat(),
            "batch_id": batch_id,
            "seen_count": seen_count,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii")


def _decode_task_group_cursor(
    cursor: str,
    *,
    session_id: str,
) -> tuple[datetime, str, int]:
    if not cursor or len(cursor.encode("utf-8")) > _TASK_CURSOR_MAXIMUM_BYTES:
        raise ValueError("task group cursor is invalid")
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("task group cursor is invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "v",
        "kind",
        "session_id",
        "first_accepted_at",
        "batch_id",
        "seen_count",
    }:
        raise ValueError("task group cursor is invalid")
    if (
        value["v"] != 1
        or value["kind"] != "task-groups"
        or value["session_id"] != session_id
    ):
        raise ValueError("task group cursor is invalid")
    batch_id = value["batch_id"]
    seen_count = value["seen_count"]
    if not isinstance(batch_id, str) or not batch_id:
        raise ValueError("task group cursor is invalid")
    if (
        not isinstance(seen_count, int)
        or isinstance(seen_count, bool)
        or seen_count < 0
    ):
        raise ValueError("task group cursor is invalid")
    try:
        first_accepted_at = datetime.fromisoformat(str(value["first_accepted_at"]))
    except ValueError as exc:
        raise ValueError("task group cursor is invalid") from exc
    if first_accepted_at.tzinfo is None:
        raise ValueError("task group cursor is invalid")
    return first_accepted_at, batch_id, seen_count


def _encode_task_activity_cursor(
    *, session_id: str, task_id: str, after_entry_sequence: int
) -> str:
    body = json.dumps(
        {
            "v": 1,
            "kind": "task-activities",
            "session_id": session_id,
            "task_id": task_id,
            "after_entry_sequence": after_entry_sequence,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii")


def _decode_task_activity_cursor(cursor: str, *, session_id: str, task_id: str) -> int:
    if not cursor or len(cursor.encode("utf-8")) > _TASK_CURSOR_MAXIMUM_BYTES:
        raise ValueError("task activity cursor is invalid")
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("task activity cursor is invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "v",
        "kind",
        "session_id",
        "task_id",
        "after_entry_sequence",
    }:
        raise ValueError("task activity cursor is invalid")
    sequence = value["after_entry_sequence"]
    if (
        value["v"] != 1
        or value["kind"] != "task-activities"
        or value["session_id"] != session_id
        or value["task_id"] != task_id
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
    ):
        raise ValueError("task activity cursor is invalid")
    return sequence


__all__ = [
    "HostSessionHandle",
    "LocalSessionController",
    "SessionWorkspaceKind",
]
