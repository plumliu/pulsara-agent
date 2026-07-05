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
from urllib.parse import SplitResult, urlsplit, urlunsplit
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


class McpRequestSourceMethod(StrEnum):
    TOOL_CALL = "tools/call"
    RESOURCE_READ = "resources/read"
    PROMPT_GET = "prompts/get"


MAX_MCP_INPUT_REQUIRED_ROUNDS = 3


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
    follow_redirects: bool = False

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("MCP streamable HTTP URL is required")
        object.__setattr__(self, "url", self.url.strip())
        if not (self.url.startswith("http://") or self.url.startswith("https://")):
            raise ValueError("MCP streamable HTTP URL must use http:// or https://")
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
class McpDiscoveredResource:
    server_id: str
    uri: str
    name: str
    description: str = ""
    mime_type: str | None = None
    size: int | None = None

    def __post_init__(self) -> None:
        if not self.server_id.strip():
            raise ValueError("MCP discovered resource requires server_id")
        if not self.uri.strip():
            raise ValueError("MCP discovered resource requires uri")
        object.__setattr__(self, "server_id", normalize_mcp_identifier(self.server_id))
        object.__setattr__(self, "uri", self.uri.strip())
        object.__setattr__(self, "name", self.name.strip() or self.uri.strip())
        object.__setattr__(self, "description", self.description.strip())


@dataclass(frozen=True, slots=True)
class McpDiscoveredResourceTemplate:
    server_id: str
    uri_template: str
    name: str
    description: str = ""
    mime_type: str | None = None

    def __post_init__(self) -> None:
        if not self.server_id.strip():
            raise ValueError("MCP discovered resource template requires server_id")
        if not self.uri_template.strip():
            raise ValueError("MCP discovered resource template requires uri_template")
        object.__setattr__(self, "server_id", normalize_mcp_identifier(self.server_id))
        object.__setattr__(self, "uri_template", self.uri_template.strip())
        object.__setattr__(self, "name", self.name.strip() or self.uri_template.strip())
        object.__setattr__(self, "description", self.description.strip())


@dataclass(frozen=True, slots=True)
class McpDiscoveredPrompt:
    server_id: str
    name: str
    description: str = ""
    arguments: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.server_id.strip():
            raise ValueError("MCP discovered prompt requires server_id")
        if not self.name.strip():
            raise ValueError("MCP discovered prompt requires name")
        object.__setattr__(self, "server_id", normalize_mcp_identifier(self.server_id))
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "description", self.description.strip())
        object.__setattr__(self, "arguments", tuple(dict(argument) for argument in self.arguments))


@dataclass(frozen=True, slots=True)
class McpServerSnapshot:
    config: McpServerConfig
    status: McpServerStatus
    tools: tuple[McpDiscoveredTool, ...] = ()
    resources: tuple[McpDiscoveredResource, ...] = ()
    resource_templates: tuple[McpDiscoveredResourceTemplate, ...] = ()
    prompts: tuple[McpDiscoveredPrompt, ...] = ()
    message: str | None = None
    generation: int = 0
    protocol_version: str | None = None
    server_info: dict[str, Any] = field(default_factory=dict)
    instructions: str | None = None
    diagnostics: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", tuple(self._filtered_tools()))
        object.__setattr__(self, "resources", tuple(self.resources))
        object.__setattr__(self, "resource_templates", tuple(self.resource_templates))
        object.__setattr__(self, "prompts", tuple(self.prompts))
        object.__setattr__(self, "server_info", dict(self.server_info))
        object.__setattr__(self, "diagnostics", tuple(dict(item) for item in self.diagnostics))

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


@dataclass(frozen=True, slots=True)
class McpOriginalRequest:
    source_method: McpRequestSourceMethod
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    resource_uri: str | None = None
    prompt_name: str | None = None
    prompt_arguments: dict[str, str] | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"source_method": self.source_method.value}
        if self.tool_name is not None:
            payload["tool_name"] = self.tool_name
        if self.arguments is not None:
            payload["arguments"] = dict(self.arguments)
        if self.resource_uri is not None:
            payload["resource_uri"] = self.resource_uri
        if self.prompt_name is not None:
            payload["prompt_name"] = self.prompt_name
        if self.prompt_arguments is not None:
            payload["prompt_arguments"] = dict(self.prompt_arguments)
        return payload


@dataclass(frozen=True, slots=True)
class McpInputRequestDTO:
    key: str
    method: str
    params: dict[str, Any]

    def to_dict(self) -> dict[str, object]:
        return {"key": self.key, "method": self.method, "params": dict(self.params)}


@dataclass(frozen=True, slots=True)
class McpInputRequired:
    interaction_id: str
    server_id: str
    protocol_version: str | None
    request_state: str | None
    input_requests: tuple[McpInputRequestDTO, ...]
    original_request: McpOriginalRequest
    round_count: int = 1
    deadline_monotonic: float | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "interaction_id": self.interaction_id,
            "kind": "mcp_input_required",
            "server_id": self.server_id,
            "protocol_version": self.protocol_version,
            "request_state": self.request_state,
            "input_requests": [request.to_dict() for request in self.input_requests],
            "original_request": self.original_request.to_dict(),
            "round_count": self.round_count,
        }
        if self.deadline_monotonic is not None:
            payload["deadline_monotonic"] = self.deadline_monotonic
        return payload


@dataclass(frozen=True, slots=True)
class McpInputRequiredResolution:
    interaction_id: str
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    cancelled: bool = False
    tool_call_id: str | None = None
    input_requests: tuple[McpInputRequestDTO, ...] = ()
    round_count: int = 1
    deadline_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class McpContentArtifact:
    role: str
    media_type: str
    text: str | None = None
    data: bytes | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.text is None) == (self.data is None):
            raise ValueError("McpContentArtifact requires exactly one of text or data")


@dataclass(frozen=True, slots=True)
class McpToolResult:
    output: str
    is_error: bool = False
    structured_content: Any = None
    artifacts: tuple[McpContentArtifact, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


_SAFE_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9_-]+")
MAX_MCP_MODEL_TOOL_NAME_CHARS = 64
_MCP_TOOL_NAME_OVERHEAD = len("mcp__") + len("__") + len("__")
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_KEY_PATTERN = r"x-api-key|api[_-]?key|access[_-]?token|auth[_-]?token|bearer[_-]?token|password|secret|token"
_AUTH_HEADER_RE = re.compile(r"(?i)\b(authorization\s*:\s*)((?:Bearer|Basic)\s+)?([^\s,;}]+)")
_SECRET_QUOTED_VALUE_RE = re.compile(
    rf"(?i)([\"']?(?:{_SECRET_KEY_PATTERN})[\"']?\s*:\s*)([\"'])([^\r\n\"']*)([\"'])"
)
_SECRET_COLON_RE = re.compile(rf"(?i)\b({_SECRET_KEY_PATTERN})\s*:\s*([^\s,;}}]+)")
_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?i)\b({_SECRET_KEY_PATTERN})=([^\s&]+)"
)


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
    budget = max_length - _MCP_TOOL_NAME_OVERHEAD - len(digest)
    if budget < 2:
        return f"mcp__{digest}"[:max_length]
    server_budget = max(1, budget // 2)
    tool_budget = max(1, budget - server_budget)
    if server_budget + tool_budget > budget:
        tool_budget = max(1, budget - server_budget)
    return f"mcp__{safe_server[:server_budget]}__{safe_tool[:tool_budget]}__{digest}"


def redact_mcp_error_message(message: object) -> str:
    """Best-effort redaction for runtime MCP errors before durable events."""

    text = str(message)
    text = _HTTP_URL_RE.sub(lambda match: _redact_url_text(match.group(0)), text)
    text = _AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)}[redacted]", text)
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = _SECRET_QUOTED_VALUE_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[redacted]{match.group(4)}",
        text,
    )
    text = _SECRET_COLON_RE.sub(lambda match: f"{match.group(1)}: [redacted]", text)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    return text


def _redact_url_text(url: str) -> str:
    trailing = ""
    while url and url[-1] in ".,;:)":
        trailing = url[-1] + trailing
        url = url[:-1]
    try:
        split = urlsplit(url)
    except ValueError:
        return url + trailing
    netloc = split.netloc
    if "@" in netloc:
        netloc = f"[redacted]@{netloc.rsplit('@', 1)[1]}"
    query = "[redacted]" if split.query else ""
    fragment = "[redacted]" if split.fragment else ""
    return urlunsplit(
        SplitResult(
            scheme=split.scheme,
            netloc=netloc,
            path=split.path,
            query=query,
            fragment=fragment,
        )
    ) + trailing


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
