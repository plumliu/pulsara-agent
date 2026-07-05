"""MCP server config store and user/workspace merge helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from pulsara_agent.runtime.mcp.types import (
    McpServerConfig,
    McpStdioConfig,
    McpStreamableHttpConfig,
    normalize_mcp_identifier,
)


DEFAULT_USER_MCP_CONFIG = Path.home() / ".pulsara" / "mcp.yaml"
WORKSPACE_MCP_CONFIG = ".pulsara/mcp.yaml"


@dataclass(frozen=True, slots=True)
class McpConfigSource:
    path: Path
    scope: str


@dataclass(frozen=True, slots=True)
class McpConfigStore:
    path: Path = DEFAULT_USER_MCP_CONFIG

    def load(self) -> tuple[McpServerConfig, ...]:
        return tuple(_config_from_entry(server_id, entry) for server_id, entry in _load_raw(self.path).items())

    def upsert(self, config: McpServerConfig) -> None:
        raw = _load_raw(self.path)
        raw[config.server_id] = _entry_from_config(config)
        _write_raw(self.path, raw)

    def remove(self, server_id: str) -> bool:
        raw = _load_raw(self.path)
        normalized = normalize_mcp_identifier(server_id)
        existed = normalized in raw
        raw.pop(normalized, None)
        _write_raw(self.path, raw)
        return existed

    def set_enabled(self, server_id: str, enabled: bool) -> bool:
        raw = _load_raw(self.path)
        normalized = normalize_mcp_identifier(server_id)
        if normalized not in raw:
            return False
        raw[normalized]["enabled"] = bool(enabled)
        _write_raw(self.path, raw)
        return True


def load_mcp_server_configs(
    *,
    workspace_root: Path | None = None,
    user_config_path: Path = DEFAULT_USER_MCP_CONFIG,
) -> tuple[McpServerConfig, ...]:
    merged: dict[str, McpServerConfig] = {}
    for source in mcp_config_sources(workspace_root=workspace_root, user_config_path=user_config_path):
        for server_id, entry in _load_raw(source.path).items():
            config = _config_from_entry(server_id, entry)
            merged[config.server_id] = config
    return tuple(merged[server_id] for server_id in sorted(merged))


def mcp_config_sources(
    *,
    workspace_root: Path | None = None,
    user_config_path: Path = DEFAULT_USER_MCP_CONFIG,
) -> tuple[McpConfigSource, ...]:
    sources = [McpConfigSource(path=user_config_path.expanduser(), scope="user")]
    if workspace_root is not None:
        sources.append(McpConfigSource(path=workspace_root.expanduser().resolve() / WORKSPACE_MCP_CONFIG, scope="workspace"))
    return tuple(sources)


def _load_raw(path: Path) -> dict[str, dict[str, Any]]:
    path = path.expanduser()
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError(f"MCP config path is not a file: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
    else:
        payload = yaml.safe_load(text)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"MCP config must be an object: {path}")
    servers = payload.get("servers", payload)
    if not isinstance(servers, dict):
        raise ValueError(f"MCP config 'servers' must be an object: {path}")
    result: dict[str, dict[str, Any]] = {}
    for server_id, entry in servers.items():
        if not isinstance(entry, dict):
            raise ValueError(f"MCP server entry must be an object: {server_id}")
        result[str(server_id)] = dict(entry)
    return result


def _write_raw(path: Path, raw: dict[str, dict[str, Any]]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"servers": {server_id: raw[server_id] for server_id in sorted(raw)}}
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _config_from_entry(server_id: str, entry: dict[str, Any]) -> McpServerConfig:
    transport_kind = str(entry.get("transport") or entry.get("type") or "").strip()
    if transport_kind in {"stdio", ""} and "command" in entry:
        transport = McpStdioConfig(
            command=str(entry["command"]),
            args=tuple(str(arg) for arg in entry.get("args") or ()),
            env={str(k): str(v) for k, v in dict(entry.get("env") or {}).items()},
            cwd=Path(entry["cwd"]).expanduser() if entry.get("cwd") else None,
        )
    elif transport_kind in {"streamable_http", "http", "remote"} or "url" in entry:
        transport = McpStreamableHttpConfig(
            url=str(entry["url"]),
            bearer_token_env_var=entry.get("bearer_token_env_var"),
            headers={str(k): str(v) for k, v in dict(entry.get("headers") or {}).items()},
            env_headers={str(k): str(v) for k, v in dict(entry.get("env_headers") or {}).items()},
            follow_redirects=bool(entry.get("follow_redirects", False)),
        )
    else:
        raise ValueError(f"MCP server {server_id!r} requires stdio command or HTTP url")
    return McpServerConfig(
        server_id=server_id,
        transport=transport,
        enabled=bool(entry.get("enabled", True)),
        required=bool(entry.get("required", False)),
        startup_timeout_ms=int(entry.get("startup_timeout_ms", 10_000)),
        tool_timeout_ms=int(entry.get("tool_timeout_ms", 30_000)),
        supports_parallel_tool_calls=bool(entry.get("supports_parallel_tool_calls", False)),
        enabled_tools=tuple(entry["enabled_tools"]) if entry.get("enabled_tools") else None,
        disabled_tools=tuple(entry.get("disabled_tools") or ()),
        default_approval_mode=entry.get("default_approval_mode"),
    )


def _entry_from_config(config: McpServerConfig) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "enabled": config.enabled,
        "required": config.required,
        "startup_timeout_ms": config.startup_timeout_ms,
        "tool_timeout_ms": config.tool_timeout_ms,
        "supports_parallel_tool_calls": config.supports_parallel_tool_calls,
    }
    if config.enabled_tools is not None:
        entry["enabled_tools"] = list(config.enabled_tools)
    if config.disabled_tools:
        entry["disabled_tools"] = list(config.disabled_tools)
    if config.default_approval_mode:
        entry["default_approval_mode"] = config.default_approval_mode
    transport = config.transport
    if isinstance(transport, McpStdioConfig):
        entry.update(
            {
                "transport": "stdio",
                "command": transport.command,
                "args": list(transport.args),
            }
        )
        if transport.env:
            entry["env"] = dict(transport.env)
        if transport.cwd is not None:
            entry["cwd"] = str(transport.cwd)
    else:
        entry.update(
            {
                "transport": "streamable_http",
                "url": transport.url,
                "follow_redirects": transport.follow_redirects,
            }
        )
        if transport.bearer_token_env_var:
            entry["bearer_token_env_var"] = transport.bearer_token_env_var
        if transport.headers:
            entry["headers"] = dict(transport.headers)
        if transport.env_headers:
            entry["env_headers"] = dict(transport.env_headers)
    return entry
