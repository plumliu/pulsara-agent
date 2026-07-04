"""Typed MCP configuration and discovery DTOs.

These types are deliberately small and Pulsara-owned.  They model the MCP
surface we need for capability exposure and execution without importing an MCP
SDK into the runtime core.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


class McpServerTransportKind(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class McpServerStatus(StrEnum):
    DISABLED = "disabled"
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    NEEDS_AUTH = "needs_auth"
    DEGRADED = "degraded"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class McpStdioConfig:
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    cwd: Path | None = None

    def __post_init__(self) -> None:
        if not self.command.strip():
            raise ValueError("MCP stdio command is required")
        object.__setattr__(self, "args", tuple(str(arg) for arg in self.args))
        object.__setattr__(self, "env", MappingProxyType({str(k): str(v) for k, v in self.env.items()}))
        if self.cwd is not None:
            object.__setattr__(self, "cwd", self.cwd.expanduser())


@dataclass(frozen=True, slots=True)
class McpStreamableHttpConfig:
    url: str
    bearer_token_env_var: str | None = None
    headers: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    env_headers: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("MCP streamable HTTP URL is required")
        object.__setattr__(self, "url", self.url.strip())
        object.__setattr__(self, "headers", MappingProxyType({str(k): str(v) for k, v in self.headers.items()}))
        object.__setattr__(
            self,
            "env_headers",
            MappingProxyType({str(k): str(v) for k, v in self.env_headers.items()}),
        )
        if self.bearer_token_env_var is not None and not self.bearer_token_env_var.strip():
            raise ValueError("bearer_token_env_var cannot be empty")


McpTransportConfig = McpStdioConfig | McpStreamableHttpConfig


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    server_id: str
    transport: McpTransportConfig
    enabled: bool = True
    required: bool = False
    startup_timeout_ms: int = 10_000
    tool_timeout_ms: int = 30_000
    supports_parallel_tool_calls: bool = False
    enabled_tools: tuple[str, ...] | None = None
    disabled_tools: tuple[str, ...] = ()
    default_approval_mode: str | None = None

    def __post_init__(self) -> None:
        normalized_id = normalize_mcp_identifier(self.server_id)
        if not normalized_id:
            raise ValueError("MCP server_id is required")
        if self.startup_timeout_ms <= 0:
            raise ValueError("startup_timeout_ms must be positive")
        if self.tool_timeout_ms <= 0:
            raise ValueError("tool_timeout_ms must be positive")
        object.__setattr__(self, "server_id", normalized_id)
        if self.enabled_tools is not None:
            object.__setattr__(self, "enabled_tools", tuple(dict.fromkeys(self.enabled_tools)))
        object.__setattr__(self, "disabled_tools", tuple(dict.fromkeys(self.disabled_tools)))

    @property
    def transport_kind(self) -> McpServerTransportKind:
        if isinstance(self.transport, McpStdioConfig):
            return McpServerTransportKind.STDIO
        return McpServerTransportKind.STREAMABLE_HTTP


@dataclass(frozen=True, slots=True)
class McpToolAnnotations:
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None
    open_world_hint: bool | None = None
    title: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "McpToolAnnotations":
        if not payload:
            return cls()
        return cls(
            read_only_hint=_optional_bool(payload.get("readOnlyHint") or payload.get("read_only_hint")),
            destructive_hint=_optional_bool(payload.get("destructiveHint") or payload.get("destructive_hint")),
            open_world_hint=_optional_bool(payload.get("openWorldHint") or payload.get("open_world_hint")),
            title=_optional_str(payload.get("title")),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "read_only_hint": self.read_only_hint,
            "destructive_hint": self.destructive_hint,
            "open_world_hint": self.open_world_hint,
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class McpDiscoveredTool:
    server_id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: McpToolAnnotations = field(default_factory=McpToolAnnotations)

    def __post_init__(self) -> None:
        if not self.server_id.strip():
            raise ValueError("MCP discovered tool requires server_id")
        if not self.name.strip():
            raise ValueError("MCP discovered tool requires name")
        object.__setattr__(self, "server_id", normalize_mcp_identifier(self.server_id))
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "description", self.description.strip() or f"MCP tool {self.name}")
        schema = dict(self.input_schema or {})
        if schema.get("type") != "object":
            schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        object.__setattr__(self, "input_schema", schema)


@dataclass(frozen=True, slots=True)
class McpServerSnapshot:
    config: McpServerConfig
    status: McpServerStatus
    tools: tuple[McpDiscoveredTool, ...] = ()
    message: str | None = None
    generation: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", tuple(self._filtered_tools()))

    def _filtered_tools(self) -> tuple[McpDiscoveredTool, ...]:
        if not self.config.enabled:
            return ()
        disabled = set(self.config.disabled_tools)
        enabled = set(self.config.enabled_tools or ())
        tools = []
        for tool in self.tools:
            if tool.name in disabled:
                continue
            if enabled and tool.name not in enabled:
                continue
            tools.append(tool)
        return tuple(tools)


_SAFE_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9_-]+")
MAX_MCP_MODEL_TOOL_NAME_CHARS = 64


def normalize_mcp_identifier(value: str) -> str:
    normalized = _SAFE_IDENTIFIER_RE.sub("_", str(value).strip()).strip("_")
    return normalized


def mangle_mcp_tool_name(server_id: str, tool_name: str, *, max_length: int = MAX_MCP_MODEL_TOOL_NAME_CHARS) -> str:
    safe_server = normalize_mcp_identifier(server_id) or "server"
    safe_tool = normalize_mcp_identifier(tool_name) or "tool"
    full = f"mcp__{safe_server}__{safe_tool}"
    if len(full) <= max_length:
        return full
    digest = hashlib.sha256(f"{server_id}\0{tool_name}".encode("utf-8")).hexdigest()[:10]
    budget = max_length - len("mcp____") - len(digest)
    server_budget = max(8, budget // 2)
    tool_budget = max(8, budget - server_budget)
    return f"mcp__{safe_server[:server_budget]}__{safe_tool[:tool_budget]}__{digest}"[:max_length]


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

