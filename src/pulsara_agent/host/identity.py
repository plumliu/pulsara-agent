"""Workspace identity resolution for product hosts."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pulsara_agent.memory.scope import MemoryDomainContext, workspace_scope

WorkspaceKind = Literal["project", "transient"]


@dataclass(frozen=True, slots=True)
class HostWorkspaceInput:
    workspace_kind: WorkspaceKind
    workspace_root: Path | str | None = None
    display_label: str | None = None
    memory_domain_id: str = "u_local"
    cleanup_workspace_root_on_close: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedWorkspace:
    workspace_kind: WorkspaceKind
    workspace_root: Path
    display_label: str
    memory_domain: MemoryDomainContext
    workspace_scope: str | None
    workspace_key: str
    cleanup_workspace_root_on_close: bool = False


def normalize_workspace_kind(raw: str) -> WorkspaceKind:
    value = raw.strip().lower()
    if value in {"project", "transient"}:
        return value  # type: ignore[return-value]
    raise ValueError("workspace_kind must be 'project' or 'transient'")


def resolve_workspace(
    workspace: HostWorkspaceInput,
    *,
    scratch_root: Path | str | None = None,
) -> ResolvedWorkspace:
    kind = normalize_workspace_kind(workspace.workspace_kind)
    if kind == "project":
        root = _resolve_project_root(workspace.workspace_root)
        stable_key = root.as_posix()
        label = _display_label(workspace.display_label, default=root.name or root.as_posix())
        domain = MemoryDomainContext(
            memory_domain_id=workspace.memory_domain_id,
            workspace_kind="project",
            stable_project_key=stable_key,
            workspace_label=label,
        )
        scope = workspace_scope(stable_key)
        return ResolvedWorkspace(
            workspace_kind="project",
            workspace_root=root,
            display_label=label,
            memory_domain=domain,
            workspace_scope=scope,
            workspace_key=scope,
        )

    root, host_created_root = _resolve_transient_root(workspace.workspace_root, scratch_root=scratch_root)
    label = _display_label(workspace.display_label, default="Scratch")
    domain = MemoryDomainContext(
        memory_domain_id=workspace.memory_domain_id,
        workspace_kind="transient",
        workspace_label=label,
    )
    return ResolvedWorkspace(
        workspace_kind="transient",
        workspace_root=root,
        display_label=label,
        memory_domain=domain,
        workspace_scope=None,
        workspace_key=f"transient:{uuid4().hex}",
        cleanup_workspace_root_on_close=host_created_root and workspace.cleanup_workspace_root_on_close,
    )


def _resolve_project_root(value: Path | str | None) -> Path:
    if value is None:
        raise ValueError("project workspace requires workspace_root")
    root = Path(value).expanduser().resolve()
    if not root.exists():
        raise ValueError(f"project workspace_root does not exist: {root}")
    if not root.is_dir():
        raise ValueError(f"project workspace_root is not a directory: {root}")
    return root


def _resolve_transient_root(
    value: Path | str | None,
    *,
    scratch_root: Path | str | None,
) -> tuple[Path, bool]:
    if value is not None:
        root = Path(value).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise ValueError(f"transient workspace_root is not a directory: {root}")
        return root, False
    base = Path(scratch_root).expanduser().resolve() if scratch_root is not None else Path(tempfile.gettempdir())
    root = base / f"pulsara-transient-{uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    return root, True


def _display_label(value: str | None, *, default: str) -> str:
    label = (value or "").strip()
    return label or default
