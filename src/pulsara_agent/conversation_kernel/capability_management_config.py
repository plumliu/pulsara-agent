"""Adapt public selections to the native configuration and overlay owners.

The native parser owns configuration validity, credential bindings and defaults.
This adapter only selects explicitly edited public fields; it never persists a
second configuration or makes public candidates into complete stored entries.
"""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from pulsara_agent.capability.mcp_management import auth_to_entry, config_to_entry
from pulsara_agent.mcp_credentials import (
    ManagedLocalCredentialReference,
    McpCredentialBinding,
    secret_to_dict,
)
from pulsara_agent.plugins.mcp_connection import (
    PluginMcpConnectionOverlay,
    overlay_to_dict,
    overlay_from_dict,
)


def selected_auth(current: Mapping[str, object], selection, *, owner):
    kind = selection.get("type")
    if kind is None or kind == current.get("type"):
        return deepcopy(dict(current))
    if kind == "bearer":
        return {
            "type": kind,
            "reference": secret_to_dict(
                ManagedLocalCredentialReference(McpCredentialBinding(owner, "bearer"))
            ),
        }
    return {"type": kind, **({"headers": {}} if kind == "static_headers" else {})}


def local_config_candidate(current, patch, *, owner):
    if not isinstance(patch, Mapping):
        raise ValueError("独立 MCP 连接的 config 必须为对象，不能恢复包默认值。")
    result = config_to_entry(current) if current is not None else {}
    old_transport = result.get("transport", {})
    edited = patch.get("transport", {})
    kind = edited.get("type", old_transport.get("type"))
    switched = bool(old_transport and kind != old_transport["type"])
    if switched:
        required = "command" if kind == "stdio" else "endpoint"
        if required not in edited:
            raise ValueError(f"切换 MCP 传输必须提供 transport.{required}。")
        result["transport"] = dict(edited)
        result["public_headers"] = {}
        if kind == "stdio":
            result["auth"] = {"type": "none"}
    elif "transport" in patch:
        # Nested fields merge; env itself remains a complete replacement map.
        result["transport"] = {**old_transport, **deepcopy(dict(edited))}
    for name in ("enabled", "public_headers"):
        if name in patch:
            result[name] = deepcopy(patch[name])
    if "auth" in patch:
        result["auth"] = selected_auth(
            result.get("auth", {"type": "none"}), patch["auth"], owner=owner
        )
    if current is None:
        result.setdefault("enabled", True)
        result.setdefault("auth", {"type": "none"})
        result.setdefault("scope_policy", "ROOT_AND_SUBAGENTS")
    return result


def plugin_config_candidate(server, current, old_overlay, patch, *, owner):
    if patch is None:
        return None
    if not isinstance(patch, Mapping):
        raise ValueError("Plugin 连接 config 必须为对象或 null。")
    if "enabled" in patch:
        raise ValueError("Plugin 连接不单独启停；请使用 SET_PLUGIN_ENABLED。")
    transport = patch.get("transport", {})
    if set(transport) & {"type", "command", "args", "cwd"}:
        raise ValueError("Plugin 的传输类型、命令、参数和工作目录由包定义，不能修改。")
    result = overlay_to_dict(
        old_overlay
        or PluginMcpConnectionOverlay(server.local_server_id, current.transport.kind.value)
    )
    stdio = server.kind.value == "stdio"
    if stdio and ("endpoint" in transport or "public_headers" in patch):
        raise ValueError("Plugin stdio 连接不接受 HTTP 地址或 Header。")
    if not stdio and "env" in transport:
        raise ValueError("Plugin HTTP 连接不接受进程环境变量。")
    if "env" in transport:
        result["environment"] = deepcopy(transport["env"])
    if "endpoint" in transport:
        result["endpoint"] = transport["endpoint"]
    if "public_headers" in patch:
        result["public_headers"] = deepcopy(patch["public_headers"])
    if "auth" in patch:
        result["auth"] = selected_auth(
            auth_to_entry(current.auth), patch["auth"], owner=owner
        )
    return overlay_from_dict(result)
