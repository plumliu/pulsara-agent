"""Durable session manifest helpers for conversation resume.

The runtime event log is the canonical transcript.  This module only manages the
small queryable manifest stored in ``sessions.metadata`` so product hosts can
list, resume, detach, and explicitly close durable conversations without adding a
new table in V1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.host.identity import HostWorkspaceInput, ResolvedWorkspace
from pulsara_agent.llm import ModelRole
from pulsara_agent.runtime.permission import (
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionProfile,
    TerminalAccess,
    mode_for_policy,
    preset_to_policy,
)
from pulsara_agent.storage import RUNTIME_TRUTH_SCHEMA_SQL


RESUME_SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class SessionManifest:
    runtime_session_id: str
    conversation_id: str
    workspace_kind: str
    workspace_root: str
    display_label: str
    memory_domain_id: str
    model_role: str
    permission_mode: str | None
    permission_policy: dict[str, object]
    created_by: str
    created_at: str | None
    last_active_at: str | None
    closed_at: str | None
    archived: bool
    metadata: dict[str, Any]

    @property
    def resumable(self) -> bool:
        return not self.archived and self.closed_at is None

    def to_workspace_input(self) -> HostWorkspaceInput:
        return HostWorkspaceInput(
            workspace_kind=self.workspace_kind,  # type: ignore[arg-type]
            workspace_root=Path(self.workspace_root),
            display_label=self.display_label,
            memory_domain_id=self.memory_domain_id,
        )


@dataclass(frozen=True, slots=True)
class ResumableSessionSummary:
    runtime_session_id: str
    conversation_id: str
    workspace_kind: str
    workspace_root: str
    display_label: str
    memory_domain_id: str
    model_role: str
    permission_mode: str | None
    created_at: str | None
    last_active_at: str | None
    closed_at: str | None
    archived: bool
    latest_run_status: str | None
    latest_run_id: str | None

    @property
    def resumable(self) -> bool:
        return not self.archived and self.closed_at is None

    def to_dict(self) -> dict[str, object]:
        return {
            "runtime_session_id": self.runtime_session_id,
            "conversation_id": self.conversation_id,
            "workspace_kind": self.workspace_kind,
            "workspace_root": self.workspace_root,
            "display_label": self.display_label,
            "memory_domain_id": self.memory_domain_id,
            "model_role": self.model_role,
            "permission_mode": self.permission_mode,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "closed_at": self.closed_at,
            "archived": self.archived,
            "latest_run_status": self.latest_run_status,
            "latest_run_id": self.latest_run_id,
            "resumable": self.resumable,
        }


class SessionManifestStore:
    """Read/write facade for the V1 resume manifest in Postgres."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def ensure_schema(self) -> None:
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(RUNTIME_TRUTH_SCHEMA_SQL)

    def upsert_open_manifest(
        self,
        *,
        runtime_session_id: str,
        conversation_id: str,
        workspace: ResolvedWorkspace,
        model_role: ModelRole,
        permission_policy: EffectivePermissionPolicy,
        created_by: str,
    ) -> SessionManifest:
        self.ensure_schema()
        now = utc_now_iso()
        existing = self.get(runtime_session_id, ensure_schema=False)
        existing_metadata = existing.metadata if existing is not None else {}
        created_at = existing.created_at if existing is not None else now
        permission_mode = mode_for_policy(permission_policy)
        metadata = _merged_manifest_metadata(
            existing_metadata,
            conversation_id=conversation_id,
            workspace=workspace,
            model_role=model_role.value,
            permission_mode=permission_mode.value if permission_mode is not None else None,
            permission_policy=permission_policy.to_dict(),
            created_by=created_by,
            created_at=created_at,
            last_active_at=now,
            closed_at=None,
            archived=False,
        )
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into sessions (id, workspace_root, metadata)
                    values (%s, %s, %s)
                    on conflict (id) do update
                    set workspace_root = excluded.workspace_root,
                        metadata = excluded.metadata
                    """,
                    (runtime_session_id, str(workspace.workspace_root), Jsonb(metadata)),
                )
        manifest = self.get(runtime_session_id)
        assert manifest is not None
        return manifest

    def touch(self, runtime_session_id: str) -> None:
        self.ensure_schema()
        now = utc_now_iso()
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    update sessions
                    set metadata = jsonb_set(
                        jsonb_set(metadata, '{resume_schema_version}', to_jsonb(%s::int), true),
                        '{lifecycle,last_active_at}', to_jsonb(%s::text), true
                    )
                    where id = %s
                    """,
                    (RESUME_SCHEMA_VERSION, now, runtime_session_id),
                )

    def mark_closed(self, runtime_session_id: str) -> None:
        self.ensure_schema()
        now = utc_now_iso()
        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    update sessions
                    set metadata = jsonb_set(
                        jsonb_set(metadata, '{resume_schema_version}', to_jsonb(%s::int), true),
                        '{lifecycle,closed_at}', to_jsonb(%s::text), true
                    )
                    where id = %s
                    """,
                    (RESUME_SCHEMA_VERSION, now, runtime_session_id),
                )

    def get(self, runtime_session_id: str, *, ensure_schema: bool = True) -> SessionManifest | None:
        if ensure_schema:
            self.ensure_schema()
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select id, workspace_root, created_at, metadata
                    from sessions
                    where id = %s
                    """,
                    (runtime_session_id,),
                )
                row = cursor.fetchone()
        return _manifest_from_row(row) if row is not None else None

    def list_resumable(
        self,
        *,
        workspace_root: str | Path | None = None,
        memory_domain_id: str | None = None,
        include_closed: bool = False,
        limit: int = 20,
    ) -> list[ResumableSessionSummary]:
        self.ensure_schema()
        predicates = ["true"]
        params: list[object] = []
        if workspace_root is not None:
            predicates.append("coalesce(metadata #>> '{workspace,workspace_root}', workspace_root) = %s")
            params.append(str(Path(workspace_root).expanduser().resolve()))
        if memory_domain_id is not None:
            predicates.append("metadata #>> '{workspace,memory_domain_id}' = %s")
            params.append(memory_domain_id)
        if not include_closed:
            predicates.append("metadata #>> '{lifecycle,closed_at}' is null")
            predicates.append("coalesce((metadata #>> '{lifecycle,archived}')::boolean, false) = false")
        params.append(limit)
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    with latest_run as (
                        select distinct on (session_id)
                            session_id,
                            id as latest_run_id,
                            status as latest_run_status,
                            started_at,
                            completed_at
                        from runs
                        order by session_id, coalesce(completed_at, started_at) desc, id desc
                    ),
                    latest_event as (
                        select session_id, max(created_at) as latest_event_at
                        from agent_events
                        group by session_id
                    )
                    select
                        sessions.id,
                        sessions.workspace_root,
                        sessions.created_at,
                        sessions.metadata,
                        latest_run.latest_run_id,
                        latest_run.latest_run_status,
                        greatest(
                            sessions.created_at,
                            coalesce(latest_event.latest_event_at, sessions.created_at),
                            coalesce(latest_run.completed_at, latest_run.started_at, sessions.created_at)
                        ) as latest_activity_at
                    from sessions
                    left join latest_run on latest_run.session_id = sessions.id
                    left join latest_event on latest_event.session_id = sessions.id
                    where {' and '.join(predicates)}
                    order by
                        greatest(
                            sessions.created_at,
                            coalesce(latest_event.latest_event_at, sessions.created_at),
                            coalesce(latest_run.completed_at, latest_run.started_at, sessions.created_at)
                        ) desc,
                        sessions.id desc
                    limit %s
                    """,
                    tuple(params),
                )
                rows = cursor.fetchall()
        return [_summary_from_row(row) for row in rows]


def _merged_manifest_metadata(
    existing: dict[str, Any],
    *,
    conversation_id: str,
    workspace: ResolvedWorkspace,
    model_role: str,
    permission_mode: str | None,
    permission_policy: dict[str, object],
    created_by: str,
    created_at: str | None,
    last_active_at: str,
    closed_at: str | None,
    archived: bool,
) -> dict[str, Any]:
    metadata = dict(existing)
    metadata["resume_schema_version"] = RESUME_SCHEMA_VERSION
    metadata["conversation_id"] = conversation_id
    metadata["workspace"] = {
        "workspace_kind": workspace.workspace_kind,
        "workspace_root": str(workspace.workspace_root),
        "display_label": workspace.display_label,
        "workspace_key": workspace.workspace_key,
        "workspace_scope": workspace.workspace_scope,
        "memory_domain_id": workspace.memory_domain.memory_domain_id,
    }
    metadata["runtime"] = {
        "model_role": model_role,
        "permission_mode": permission_mode,
        "permission_policy": permission_policy,
    }
    metadata["lifecycle"] = {
        "created_by": created_by,
        "created_at": created_at,
        "last_active_at": last_active_at,
        "closed_at": closed_at,
        "archived": archived,
    }
    return metadata


def permission_policy_from_manifest(manifest: SessionManifest) -> EffectivePermissionPolicy | None:
    if manifest.permission_mode is not None:
        return preset_to_policy(manifest.permission_mode)
    policy = manifest.permission_policy
    if not policy:
        return None
    return EffectivePermissionPolicy(
        profile=PermissionProfile(str(policy["profile"])),
        approval=ApprovalPolicy(str(policy["approval_policy"])),
        terminal=TerminalAccess(str(policy["terminal_access"])),
    )


def _manifest_from_row(row: dict[str, Any]) -> SessionManifest:
    metadata = _dict(row.get("metadata"))
    workspace = _dict(metadata.get("workspace"))
    runtime = _dict(metadata.get("runtime"))
    lifecycle = _dict(metadata.get("lifecycle"))
    workspace_root = str(workspace.get("workspace_root") or row.get("workspace_root") or ".")
    return SessionManifest(
        runtime_session_id=str(row["id"]),
        conversation_id=str(metadata.get("conversation_id") or f"conversation:{row['id']}"),
        workspace_kind=str(workspace.get("workspace_kind") or "project"),
        workspace_root=workspace_root,
        display_label=str(workspace.get("display_label") or Path(workspace_root).name or workspace_root),
        memory_domain_id=str(workspace.get("memory_domain_id") or "u_local"),
        model_role=str(runtime.get("model_role") or ModelRole.PRO.value),
        permission_mode=runtime.get("permission_mode") if isinstance(runtime.get("permission_mode"), str) else None,
        permission_policy=_dict(runtime.get("permission_policy")),
        created_by=str(lifecycle.get("created_by") or "unknown"),
        created_at=_str_or_none(lifecycle.get("created_at")) or _str_or_none(row.get("created_at")),
        last_active_at=_str_or_none(lifecycle.get("last_active_at")),
        closed_at=_str_or_none(lifecycle.get("closed_at")),
        archived=bool(lifecycle.get("archived", False)),
        metadata=metadata,
    )


def _summary_from_row(row: dict[str, Any]) -> ResumableSessionSummary:
    manifest = _manifest_from_row(row)
    latest = row.get("latest_activity_at")
    return ResumableSessionSummary(
        runtime_session_id=manifest.runtime_session_id,
        conversation_id=manifest.conversation_id,
        workspace_kind=manifest.workspace_kind,
        workspace_root=manifest.workspace_root,
        display_label=manifest.display_label,
        memory_domain_id=manifest.memory_domain_id,
        model_role=manifest.model_role,
        permission_mode=manifest.permission_mode,
        created_at=manifest.created_at,
        last_active_at=_str_or_none(latest) or manifest.last_active_at,
        closed_at=manifest.closed_at,
        archived=manifest.archived,
        latest_run_status=_str_or_none(row.get("latest_run_status")),
        latest_run_id=_str_or_none(row.get("latest_run_id")),
    )


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
