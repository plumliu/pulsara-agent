"""Neutral MCP configuration discovery used by the Kernel composition gate.

The Stage 3 product has no MCP supervisor or SDK owner.  This leaf only proves
whether a user/workspace configuration enables an MCP server so Kernel startup
can fail closed before acquiring session resources.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import yaml


DEFAULT_USER_MCP_CONFIG = Path.home() / ".pulsara" / "mcp.yaml"
WORKSPACE_MCP_CONFIG = ".pulsara/mcp.yaml"


@dataclass(frozen=True, slots=True)
class DetectedMcpServerConfig:
    server_id: str
    enabled: bool


def load_mcp_server_configs(
    *,
    workspace_root: Path | None = None,
    user_config_path: Path = DEFAULT_USER_MCP_CONFIG,
) -> tuple[DetectedMcpServerConfig, ...]:
    merged: dict[str, DetectedMcpServerConfig] = {}
    paths = [user_config_path.expanduser()]
    if workspace_root is not None:
        paths.append(workspace_root.expanduser().resolve() / WORKSPACE_MCP_CONFIG)
    for path in paths:
        for server_id, entry in _load_raw(path).items():
            merged[server_id] = DetectedMcpServerConfig(
                server_id=server_id,
                enabled=bool(entry.get("enabled", True)),
            )
    return tuple(merged[key] for key in sorted(merged))


def _load_raw(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError(f"MCP config path is not a file: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    payload = (
        json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    )
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"MCP config must be an object: {path}")
    servers = payload.get("servers", payload)
    if not isinstance(servers, dict):
        raise ValueError(f"MCP config 'servers' must be an object: {path}")
    result: dict[str, dict[str, Any]] = {}
    for raw_id, raw_entry in servers.items():
        server_id = str(raw_id).strip()
        if not server_id or not isinstance(raw_entry, dict):
            raise ValueError(f"MCP server entry is invalid: {raw_id!r}")
        result[server_id] = dict(raw_entry)
    return result


__all__ = ["DetectedMcpServerConfig", "load_mcp_server_configs"]
