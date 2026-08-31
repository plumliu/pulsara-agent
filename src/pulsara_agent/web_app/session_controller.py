"""Cold-safe Session control plane for the local Web application."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Literal, Mapping
from uuid import uuid4

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import (
    KernelHostCore,
    KernelHostCoreClosing,
    KernelHostSession,
    KernelSessionSummary,
)
from pulsara_agent.llm.models import ModelRole
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
        tasks = [
            _task_payload(item, dependencies[str(item["id"])]) for item in page
        ]
        total_count = (
            int(page[0]["total_count"])
            if page
            else seen_count
        )
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
        "v", "session_id", "accepted_at", "task_id", "seen_count"
    }:
        raise ValueError("task cursor is invalid")
    if value["v"] != 1 or value["session_id"] != session_id:
        raise ValueError("task cursor is invalid")
    task_id = value["task_id"]
    seen_count = value["seen_count"]
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task cursor is invalid")
    if not isinstance(seen_count, int) or isinstance(seen_count, bool) or seen_count < 0:
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
