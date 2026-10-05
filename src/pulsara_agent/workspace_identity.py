"""Workspace identity resolution for the canonical Host."""

from __future__ import annotations

import tempfile
import os
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pulsara_agent.memory.scope import MemoryDomainContext, workspace_context_id

WorkspaceKind = Literal["project", "transient"]


@dataclass(frozen=True, slots=True)
class WorkspaceAvailability:
    path: str
    outcome: Literal["AVAILABLE", "MISSING", "UNAVAILABLE"]
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "outcome": self.outcome, "reason": self.reason}


class WorkspaceUnavailable(ValueError):
    def __init__(self, observation: WorkspaceAvailability):
        self.observation = observation
        super().__init__(observation.reason or f"工作目录已丢失：{observation.path}")


def observe_workspace(root: Path | str) -> WorkspaceAvailability:
    """Observe the saved canonical path without creating or rebinding it."""
    path = Path(root)
    try:
        if not path.is_absolute() or path.resolve(strict=False) != path:
            return WorkspaceAvailability(str(path), "UNAVAILABLE", "工作目录路径身份已改变。")
        mode = path.stat().st_mode
        if not stat.S_ISDIR(mode):
            return WorkspaceAvailability(str(path), "UNAVAILABLE", "工作目录路径不是目录。")
        if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            return WorkspaceAvailability(str(path), "UNAVAILABLE", "工作目录访问权限不足。")
    except FileNotFoundError:
        return WorkspaceAvailability(str(path), "MISSING")
    except (OSError, RuntimeError) as exc:
        return WorkspaceAvailability(str(path), "UNAVAILABLE", str(exc))
    return WorkspaceAvailability(str(path), "AVAILABLE")


def require_workspace(root: Path | str) -> WorkspaceAvailability:
    observation = observe_workspace(root)
    if observation.outcome != "AVAILABLE":
        raise WorkspaceUnavailable(observation)
    return observation


@dataclass(frozen=True, slots=True)
class HostWorkspaceInput:
    workspace_kind: WorkspaceKind
    workspace_root: Path | str | None = None
    display_label: str | None = None
    memory_domain_id: str = "u_local"
    cleanup_workspace_root_on_close: bool = False
    trust_workspace_mcp_config: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedWorkspace:
    workspace_kind: WorkspaceKind
    workspace_root: Path
    display_label: str
    memory_domain: MemoryDomainContext
    workspace_context_id: str | None
    workspace_key: str
    cleanup_workspace_root_on_close: bool = False
    trust_workspace_mcp_config: bool = False


def normalize_workspace_kind(raw: str) -> WorkspaceKind:
    value = raw.strip().lower()
    if value in {"project", "transient"}:
        return value  # type: ignore[return-value]
    raise ValueError("workspace_kind must be 'project' or 'transient'")


def resolve_workspace(
    workspace: HostWorkspaceInput,
    *,
    scratch_root: Path | str | None = None,
    intent: Literal["NEW", "EXISTING", "RESTORE", "READ"] = "NEW",
) -> ResolvedWorkspace:
    if intent not in {"NEW", "EXISTING", "RESTORE", "READ"}:
        raise ValueError("invalid workspace resolution intent")
    kind = normalize_workspace_kind(workspace.workspace_kind)
    if kind == "project":
        if workspace.display_label is not None:
            raise ValueError("project workspace does not accept display_label")
        root = _resolve_project_root(workspace.workspace_root, intent=intent)
        stable_key = root.as_posix()
        label = root.name or root.as_posix()
        domain = MemoryDomainContext(
            memory_domain_id=workspace.memory_domain_id,
            workspace_kind="project",
            stable_project_key=stable_key,
            workspace_label=label,
        )
        context_id = workspace_context_id(stable_key)
        return ResolvedWorkspace(
            workspace_kind="project",
            workspace_root=root,
            display_label=label,
            memory_domain=domain,
            workspace_context_id=context_id,
            workspace_key=context_id,
            trust_workspace_mcp_config=workspace.trust_workspace_mcp_config,
        )

    root, host_created_root = _resolve_transient_root(
        workspace.workspace_root, scratch_root=scratch_root, intent=intent
    )
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
        workspace_context_id=None,
        # A transient workspace has no project-memory context, but its physical
        # directory can still back a durable, resumable conversation.  The
        # canonical identity therefore follows that exact normalized root.
        workspace_key=(
            "transient:"
            + sha256(root.as_posix().encode("utf-8")).hexdigest()
        ),
        cleanup_workspace_root_on_close=host_created_root
        and workspace.cleanup_workspace_root_on_close,
        trust_workspace_mcp_config=workspace.trust_workspace_mcp_config,
    )


def _resolve_project_root(value: Path | str | None, *, intent: str) -> Path:
    if value is None:
        raise ValueError("project workspace requires workspace_root")
    root = Path(value).expanduser().absolute()
    if intent == "NEW":
        root = root.resolve()
    if intent not in {"RESTORE", "READ"}:
        require_workspace(root)
    return root


def _resolve_transient_root(
    value: Path | str | None,
    *,
    scratch_root: Path | str | None,
    intent: str,
) -> tuple[Path, bool]:
    if value is not None:
        root = Path(value).expanduser().absolute()
        if intent == "NEW":
            root = root.resolve()
            root.mkdir(parents=True, exist_ok=True)
        if intent not in {"RESTORE", "READ"}:
            require_workspace(root)
        return root, False
    if intent != "NEW":
        raise ValueError("existing workspace requires its saved root")
    base = (
        Path(scratch_root).expanduser().resolve()
        if scratch_root is not None
        else Path(tempfile.gettempdir())
    )
    root = base / f"pulsara-transient-{uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    return root, True


def _display_label(value: str | None, *, default: str) -> str:
    label = (value or "").strip()
    return label or default


__all__ = [
    "HostWorkspaceInput",
    "ResolvedWorkspace",
    "WorkspaceKind",
    "normalize_workspace_kind",
    "resolve_workspace",
]
