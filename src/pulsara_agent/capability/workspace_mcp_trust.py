"""Local approval for exact workspace MCP server configurations."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import Mapping

import yaml

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.mcp_config import (
    LocalConfiguredMcpRuntimeSource,
    McpLocalConfigSourceKind,
    McpServerConfig,
    load_workspace_mcp_server_configs,
    mcp_server_workspace_approval_identity,
)
from pulsara_agent.primitives.context import context_fingerprint


WORKSPACE_MCP_TRUST_DIRECTORY = "workspace-mcp-trust"
MAXIMUM_WORKSPACE_MCP_TRUST_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class WorkspaceMcpTrustEntry:
    server_id: str
    approval_identity: str

    def __post_init__(self) -> None:
        if not self.server_id or len(self.server_id.encode("utf-8")) > 128:
            raise ValueError("workspace MCP trust server id is invalid")
        if not self.approval_identity:
            raise ValueError("workspace MCP approval identity is invalid")


def workspace_mcp_trust_path(workspace_root: Path) -> Path:
    """Return one bounded approval document dedicated to one physical workspace."""

    workspace = workspace_root.expanduser().resolve(strict=False)
    identity = context_fingerprint(
        "pulsara:workspace-mcp-approval-store:v1", str(workspace)
    ).removeprefix("sha256:")
    return require_pulsara_home() / WORKSPACE_MCP_TRUST_DIRECTORY / f"{identity}.yaml"


def workspace_mcp_server_approvals(workspace_root: Path) -> dict[str, str]:
    """Return approved stable identities for one physical workspace."""

    workspace = workspace_root.expanduser().resolve(strict=False)
    entries = _load_entries(workspace_mcp_trust_path(workspace), workspace)
    if entries is None:
        return {}
    return {
        server_id: entry.approval_identity
        for server_id, entry in entries.items()
    }


def workspace_mcp_server_config_is_trusted(
    workspace_root: Path,
    config: McpServerConfig,
) -> bool:
    approvals = workspace_mcp_server_approvals(workspace_root)
    return approvals.get(config.server_id) == mcp_server_workspace_approval_identity(
        config
    )


def workspace_mcp_config_is_trusted(workspace_root: Path) -> bool:
    """Return true when every enabled entry has an exact local approval."""

    try:
        configs = load_workspace_mcp_server_configs(workspace_root)
    except (OSError, ValueError):
        return False
    if not configs:
        return False
    approvals = workspace_mcp_server_approvals(workspace_root)
    return all(
        not config.enabled
        or approvals.get(config.server_id)
        == mcp_server_workspace_approval_identity(config)
        for config in configs
    )


def approve_workspace_mcp_server_config(
    workspace_root: Path,
    config: McpServerConfig,
) -> Path:
    """Approve one current workspace value and prune obsolete approvals."""

    if not isinstance(config.runtime_source, LocalConfiguredMcpRuntimeSource) or (
        config.runtime_source.source_kind is not McpLocalConfigSourceKind.WORKSPACE
    ):
        raise ValueError("only a workspace MCP config can be approved")
    workspace = workspace_root.expanduser().resolve(strict=False)
    current_configs = load_workspace_mcp_server_configs(workspace)
    current_by_id = {item.server_id: item for item in current_configs}
    current = current_by_id.get(config.server_id)
    approval_identity = mcp_server_workspace_approval_identity(config)
    if (
        current is None
        or mcp_server_workspace_approval_identity(current) != approval_identity
    ):
        raise ValueError("MCP configuration changed; refresh before approving it")

    path = workspace_mcp_trust_path(workspace)
    entries = _load_entries(path, workspace)
    if entries is None:
        raise ValueError("项目 MCP 授权记录暂时无法读取。")
    entries = {
        server_id: entry
        for server_id, entry in entries.items()
        if server_id in current_by_id
        and entry.approval_identity
        == mcp_server_workspace_approval_identity(current_by_id[server_id])
    }
    entries[config.server_id] = WorkspaceMcpTrustEntry(
        server_id=config.server_id,
        approval_identity=approval_identity,
    )
    _write_entries(path, workspace, entries)
    return path


def remove_workspace_mcp_server_approval(
    workspace_root: Path,
    server_id: str,
) -> Path:
    workspace = workspace_root.expanduser().resolve(strict=False)
    path = workspace_mcp_trust_path(workspace)
    entries = _load_entries(path, workspace)
    if entries is None:
        # Revocation is fail-closed: a malformed file or an exact-path symlink
        # cannot preserve authority merely because its contents are unreadable.
        # Unlinking this exact path never follows a symlink target.
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return path
    entries.pop(server_id, None)
    _write_entries(path, workspace, entries)
    return path


def _write_entries(
    path: Path,
    workspace: Path,
    entries: Mapping[str, WorkspaceMcpTrustEntry],
) -> None:
    if not entries:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    encoded = yaml.safe_dump(
        {
            "workspace_path": str(workspace),
            "approvals": [
                {
                    "server_id": item.server_id,
                    "approval_identity": item.approval_identity,
                }
                for item in sorted(entries.values(), key=lambda value: value.server_id)
            ],
        },
        sort_keys=False,
        allow_unicode=True,
    ).encode("utf-8")
    if len(encoded) > MAXIMUM_WORKSPACE_MCP_TRUST_BYTES:
        raise ValueError("这个目录的 MCP 授权记录无法保存。")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _load_entries(
    path: Path,
    workspace: Path,
) -> dict[str, WorkspaceMcpTrustEntry] | None:
    if path.is_symlink():
        return None
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    if len(data) > MAXIMUM_WORKSPACE_MCP_TRUST_BYTES:
        return None
    try:
        loaded = yaml.safe_load(data.decode("utf-8"))
        raw = {} if loaded is None else loaded
        if not isinstance(raw, Mapping) or set(raw) != {
            "workspace_path",
            "approvals",
        }:
            raise ValueError("workspace MCP trust root is invalid")
        raw_path = raw.get("workspace_path")
        if not isinstance(raw_path, str) or Path(raw_path).expanduser().resolve(
            strict=False
        ) != workspace:
            raise ValueError("workspace MCP trust owner is invalid")
        values = raw.get("approvals")
        if not isinstance(values, list):
            raise ValueError("workspace MCP trust entries are invalid")
        entries: dict[str, WorkspaceMcpTrustEntry] = {}
        expected_fields = {"server_id", "approval_identity"}
        for value in values:
            if not isinstance(value, Mapping) or set(value) != expected_fields:
                raise ValueError("workspace MCP trust entry is invalid")
            server_id = value.get("server_id")
            identity = value.get("approval_identity")
            if not isinstance(server_id, str) or not isinstance(identity, str):
                raise ValueError("workspace MCP trust fields are invalid")
            entry = WorkspaceMcpTrustEntry(
                server_id=server_id,
                approval_identity=identity,
            )
            if entry.server_id in entries:
                raise ValueError("workspace MCP trust entry is duplicated")
            entries[entry.server_id] = entry
        return entries
    except (UnicodeError, ValueError, yaml.YAMLError):
        return None


__all__ = [
    "MAXIMUM_WORKSPACE_MCP_TRUST_BYTES",
    "WORKSPACE_MCP_TRUST_DIRECTORY",
    "WorkspaceMcpTrustEntry",
    "approve_workspace_mcp_server_config",
    "remove_workspace_mcp_server_approval",
    "workspace_mcp_config_is_trusted",
    "workspace_mcp_server_approvals",
    "workspace_mcp_server_config_is_trusted",
    "workspace_mcp_trust_path",
]
