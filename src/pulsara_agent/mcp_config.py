"""Typed, secret-safe MCP configuration for one Kernel Host composition."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

import yaml

from pulsara_agent.primitives.context import context_fingerprint


DEFAULT_USER_MCP_CONFIG = Path.home() / ".pulsara" / "mcp.yaml"
WORKSPACE_MCP_CONFIG = ".pulsara/mcp.yaml"
DEFAULT_MCP_TOOL_TIMEOUT_MS = 600_000
DEFAULT_MCP_REFRESH_INTERVAL_MS = 300_000
MAXIMUM_MCP_TOOL_OVERRIDES = 512
MAXIMUM_MCP_CONFIG_BYTES = 1024 * 1024
MAXIMUM_MCP_CONFIGURED_SERVERS = 64
_PROCESS_SECRET_COMMITMENT_KEY = os.urandom(32)


class McpTransportKind(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class McpHttpNetworkPolicy(StrEnum):
    PUBLIC_ONLY = "PUBLIC_ONLY"
    ALLOW_PRIVATE = "ALLOW_PRIVATE"


class McpScopePolicy(StrEnum):
    ROOT_ONLY = "ROOT_ONLY"
    ROOT_AND_SUBAGENTS = "ROOT_AND_SUBAGENTS"


class McpInvalidToolPolicy(StrEnum):
    FAIL_SERVER = "FAIL_SERVER"
    OMIT_INVALID = "OMIT_INVALID"


class McpConfiguredEffect(StrEnum):
    AUTO = "AUTO"
    READ_ONLY = "READ_ONLY"
    EXTERNAL_EFFECT = "EXTERNAL_EFFECT"


@dataclass(frozen=True, slots=True)
class StdioTransportConfig:
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    environment: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    secret_environment_refs: tuple[tuple[str, str], ...] = field(
        default=(), repr=False
    )
    kind: McpTransportKind = McpTransportKind.STDIO

    def __post_init__(self) -> None:
        if not self.command or "\x00" in self.command:
            raise ValueError("MCP stdio command is invalid")
        if any("\x00" in item for item in self.args):
            raise ValueError("MCP stdio argument contains NUL")
        _validate_unique_pairs(self.environment, "MCP stdio environment")
        _validate_unique_pairs(
            self.secret_environment_refs, "MCP stdio secret environment"
        )


@dataclass(frozen=True, slots=True)
class StreamableHttpTransportConfig:
    endpoint: str
    allow_http_localhost: bool = False
    network_policy: McpHttpNetworkPolicy = McpHttpNetworkPolicy.PUBLIC_ONLY
    proved_stateless: bool = False
    kind: McpTransportKind = McpTransportKind.STREAMABLE_HTTP

    def __post_init__(self) -> None:
        from urllib.parse import urlsplit

        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("MCP HTTP endpoint is invalid")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("MCP HTTP endpoint must not contain credentials/fragment")
        if parsed.scheme == "http":
            localhost = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if not (localhost and self.allow_http_localhost):
                raise ValueError("MCP HTTP requires HTTPS except explicit localhost")


McpTransportConfig = StdioTransportConfig | StreamableHttpTransportConfig


@dataclass(frozen=True, slots=True)
class NoAuth:
    kind: str = "none"


@dataclass(frozen=True, slots=True)
class StaticHeaderEnvironmentRefs:
    headers: tuple[tuple[str, str], ...]
    kind: str = "static_header_environment_refs"

    def __post_init__(self) -> None:
        _validate_unique_pairs(self.headers, "MCP static headers")


@dataclass(frozen=True, slots=True)
class BearerEnvironmentRef:
    environment_variable: str
    kind: str = "bearer_environment_ref"

    def __post_init__(self) -> None:
        if not _valid_env_name(self.environment_variable):
            raise ValueError("MCP bearer environment reference is invalid")


@dataclass(frozen=True, slots=True)
class UnsupportedOAuth:
    reason: str = "OAuth is not supported by Round 6 V1"
    kind: str = "unsupported_oauth"


McpAuthConfig = (
    NoAuth
    | StaticHeaderEnvironmentRefs
    | BearerEnvironmentRef
    | UnsupportedOAuth
)


@dataclass(frozen=True, slots=True)
class McpExposurePolicy:
    include_tool_names: tuple[str, ...] | None = None
    exclude_tool_names: tuple[str, ...] = ()
    invalid_tool_policy: McpInvalidToolPolicy = McpInvalidToolPolicy.FAIL_SERVER

    def __post_init__(self) -> None:
        if self.include_tool_names is not None:
            _validate_names(self.include_tool_names, "MCP included tools")
        _validate_names(self.exclude_tool_names, "MCP excluded tools")
        if self.include_tool_names is not None and set(
            self.include_tool_names
        ) & set(self.exclude_tool_names):
            raise ValueError("MCP include/exclude sets overlap")


@dataclass(frozen=True, slots=True)
class McpEffectPolicyConfig:
    default_effect: McpConfiguredEffect = McpConfiguredEffect.AUTO
    tool_effect_overrides: tuple[tuple[str, McpConfiguredEffect], ...] = ()

    def __post_init__(self) -> None:
        if len(self.tool_effect_overrides) > MAXIMUM_MCP_TOOL_OVERRIDES:
            raise ValueError("too many MCP per-tool effect overrides")
        names = tuple(name for name, _ in self.tool_effect_overrides)
        _validate_names(names, "MCP effect override tools")
        if any(effect is McpConfiguredEffect.AUTO for _, effect in self.tool_effect_overrides):
            raise ValueError("per-tool MCP effect override cannot be AUTO")


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    server_id: str
    display_name: str
    enabled: bool
    required: bool
    transport: McpTransportConfig
    auth: McpAuthConfig
    exposure_policy: McpExposurePolicy
    scope_policy: McpScopePolicy
    effect_policy: McpEffectPolicyConfig
    supports_parallel_tool_calls: bool
    stateless_http_max_in_flight: int
    catalog_refresh_interval_ms: int | None
    default_tool_timeout_ms: int
    per_tool_timeout_ms: tuple[tuple[str, int], ...]
    semantic_config_fingerprint: str
    runtime_config_fingerprint: str
    resolved_config_identity: str

    def __post_init__(self) -> None:
        if not self.server_id or not self.display_name:
            raise ValueError("MCP server identity is incomplete")
        if not 1 <= self.stateless_http_max_in_flight <= 16:
            raise ValueError("MCP stateless concurrency is out of range")
        if not 1_000 <= self.default_tool_timeout_ms <= 600_000:
            raise ValueError("MCP default tool timeout is out of range")
        if self.catalog_refresh_interval_ms is not None and not (
            30_000 <= self.catalog_refresh_interval_ms <= 86_400_000
        ):
            raise ValueError("MCP refresh interval is out of range")
        if len(self.per_tool_timeout_ms) > MAXIMUM_MCP_TOOL_OVERRIDES:
            raise ValueError("too many MCP per-tool timeout overrides")
        names = tuple(name for name, _ in self.per_tool_timeout_ms)
        _validate_names(names, "MCP timeout override tools")
        if any(not 1_000 <= value <= 600_000 for _, value in self.per_tool_timeout_ms):
            raise ValueError("MCP per-tool timeout is out of range")
        semantic, runtime, resolved = _derive_config_fingerprints(
            server_id=self.server_id,
            display_name=self.display_name,
            enabled=self.enabled,
            required=self.required,
            transport=self.transport,
            auth=self.auth,
            exposure=self.exposure_policy,
            scope_policy=self.scope_policy,
            effect=self.effect_policy,
            supports_parallel=self.supports_parallel_tool_calls,
            stateless_http_max_in_flight=self.stateless_http_max_in_flight,
            catalog_refresh_interval_ms=self.catalog_refresh_interval_ms,
            default_tool_timeout_ms=self.default_tool_timeout_ms,
            per_tool_timeout_ms=self.per_tool_timeout_ms,
        )
        if (
            self.semantic_config_fingerprint != semantic
            or self.runtime_config_fingerprint != runtime
            or self.resolved_config_identity != resolved
        ):
            raise ValueError("MCP config fingerprints do not exact-join fields")

    def resolved_headers(self) -> dict[str, str]:
        if isinstance(self.auth, NoAuth):
            return {}
        if isinstance(self.auth, UnsupportedOAuth):
            raise ValueError(self.auth.reason)
        if isinstance(self.auth, BearerEnvironmentRef):
            value = os.environ.get(self.auth.environment_variable)
            if value is None:
                raise ValueError("MCP bearer secret reference is unavailable")
            return {"Authorization": f"Bearer {value}"}
        result: dict[str, str] = {}
        for name, environment_variable in self.auth.headers:
            value = os.environ.get(environment_variable)
            if value is None:
                raise ValueError("MCP header secret reference is unavailable")
            result[name] = value
        return result


# Compatibility name retained for callers which only inspect enabled IDs.
DetectedMcpServerConfig = McpServerConfig


def load_mcp_server_configs(
    *,
    workspace_root: Path | None = None,
    user_config_path: Path = DEFAULT_USER_MCP_CONFIG,
    host_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    trust_workspace_config: bool = False,
) -> tuple[McpServerConfig, ...]:
    # User configuration is explicit local authority.  A repository-owned
    # workspace file is untrusted until this exact Host open opts in: merely
    # checking out a repository must not launch code or resolve secret refs.
    merged = _load_raw(user_config_path.expanduser())
    if workspace_root is not None:
        workspace_entries = _load_raw(
            workspace_root.expanduser().resolve() / WORKSPACE_MCP_CONFIG
        )
        if trust_workspace_config:
            merged.update(workspace_entries)
        else:
            for server_id, entry in workspace_entries.items():
                # Untrusted workspace data cannot shadow a trusted user entry.
                # Otherwise retain it for inspection, but force it disabled.
                if server_id in merged:
                    continue
                disabled = dict(entry)
                disabled["enabled"] = False
                merged[server_id] = disabled
    if host_overrides:
        for server_id, value in host_overrides.items():
            if not isinstance(server_id, str):
                raise ValueError("MCP Host override id must be a string")
            if not isinstance(value, Mapping):
                raise ValueError("MCP Host override must be an object")
            merged[server_id] = dict(value)
    return tuple(_parse_server(server_id, merged[server_id]) for server_id in sorted(merged))


def write_mcp_server_config(
    *,
    server_id: str,
    entry: Mapping[str, Any] | None,
    workspace_root: Path | None = None,
    user_config_path: Path = DEFAULT_USER_MCP_CONFIG,
) -> Path:
    path = (
        workspace_root.expanduser().resolve() / WORKSPACE_MCP_CONFIG
        if workspace_root is not None
        else user_config_path.expanduser()
    )
    raw = _load_raw(path)
    if entry is None:
        raw.pop(server_id, None)
    else:
        candidate = dict(entry)
        _parse_server(server_id, candidate)
        raw[server_id] = candidate
    encoded = yaml.safe_dump(
        {"servers": raw}, sort_keys=True, allow_unicode=True
    ).encode("utf-8")
    if len(encoded) > MAXIMUM_MCP_CONFIG_BYTES:
        raise ValueError("MCP config exceeds the byte bound")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
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
    return path


def set_mcp_server_enabled(
    *,
    server_id: str,
    enabled: bool,
    workspace_root: Path | None = None,
    user_config_path: Path = DEFAULT_USER_MCP_CONFIG,
) -> Path:
    """Edit one explicit config entry without serializing runtime discovery."""

    path = (
        workspace_root.expanduser().resolve() / WORKSPACE_MCP_CONFIG
        if workspace_root is not None
        else user_config_path.expanduser()
    )
    raw = _load_raw(path)
    entry = raw.get(server_id)
    if entry is None:
        raise KeyError(server_id)
    updated = dict(entry)
    updated["enabled"] = enabled
    return write_mcp_server_config(
        server_id=server_id,
        entry=updated,
        workspace_root=workspace_root,
        user_config_path=user_config_path,
    )


def _parse_server(server_id: str, raw: Mapping[str, Any]) -> McpServerConfig:
    server_id = server_id.strip()
    if not server_id or len(server_id.encode("utf-8")) > 128:
        raise ValueError("MCP server id is invalid")
    _reject_unknown_keys(
        raw,
        {
            "allow_http_localhost",
            "allow_private_network",
            "network_policy",
            "args",
            "auth",
            "catalog_refresh_interval_ms",
            "command",
            "cwd",
            "default_tool_timeout_ms",
            "display_name",
            "effect_policy",
            "enabled",
            "endpoint",
            "env",
            "exposure_policy",
            "follow_redirects",
            "per_tool_timeout_ms",
            "proved_stateless",
            "required",
            "scope_policy",
            "secret_env",
            "stateless_http_max_in_flight",
            "supports_parallel_tool_calls",
            "tool_timeout_ms",
            "transport",
            "url",
        },
        "MCP server",
    )
    if _boolean(raw.get("follow_redirects", False), "MCP follow_redirects"):
        raise ValueError("MCP HTTP redirects are disabled in Round 6 V1")
    transport_raw = raw.get("transport")
    if isinstance(transport_raw, str):
        kind = transport_raw.lower()
        transport_raw = (
            {
                "type": "stdio",
                "command": raw.get("command"),
                "args": raw.get("args", []),
                "cwd": raw.get("cwd"),
                "env": raw.get("env", {}),
                "secret_env": raw.get("secret_env", {}),
            }
            if kind == "stdio"
            else {
                "type": kind,
                "endpoint": raw.get("url") or raw.get("endpoint"),
                "allow_http_localhost": raw.get("allow_http_localhost", False),
                "network_policy": raw.get(
                    "network_policy",
                    (
                        "ALLOW_PRIVATE"
                        if raw.get("allow_private_network", False)
                        else "PUBLIC_ONLY"
                    ),
                ),
                "proved_stateless": raw.get("proved_stateless", False),
            }
        )
    if transport_raw is None:
        # Accept the common explicit flat spelling as input, while freezing the
        # same typed transport fact.
        transport_raw = (
            {
                "type": "stdio",
                "command": raw.get("command"),
                "args": raw.get("args", []),
                "cwd": raw.get("cwd"),
                "env": raw.get("env", {}),
                "secret_env": raw.get("secret_env", {}),
            }
            if "command" in raw
            else {
                "type": "streamable_http",
                "endpoint": raw.get("url") or raw.get("endpoint"),
                "allow_http_localhost": raw.get("allow_http_localhost", False),
                "network_policy": raw.get(
                    "network_policy",
                    (
                        "ALLOW_PRIVATE"
                        if raw.get("allow_private_network", False)
                        else "PUBLIC_ONLY"
                    ),
                ),
                "proved_stateless": raw.get("proved_stateless", False),
            }
        )
    if not isinstance(transport_raw, Mapping):
        raise ValueError("MCP transport must be an object")
    transport_type = _string(
        transport_raw.get("type", ""), "MCP transport type"
    ).lower()
    if transport_type == "stdio":
        _reject_unknown_keys(
            transport_raw,
            {"type", "command", "args", "cwd", "env", "secret_env"},
            "MCP stdio transport",
        )
        environment = _string_mapping(transport_raw.get("env", {}), "MCP env")
        secret_environment = _string_mapping(
            transport_raw.get("secret_env", {}), "MCP secret env"
        )
        transport: McpTransportConfig = StdioTransportConfig(
            command=_string(transport_raw.get("command") or "", "MCP command"),
            args=_string_list(transport_raw.get("args", []), "MCP args"),
            cwd=(
                _string(transport_raw["cwd"], "MCP cwd")
                if transport_raw.get("cwd") is not None
                else None
            ),
            environment=tuple(sorted(environment.items())),
            secret_environment_refs=tuple(sorted(secret_environment.items())),
        )
    elif transport_type in {"streamable_http", "http"}:
        _reject_unknown_keys(
            transport_raw,
            {
                "type",
                "endpoint",
                "url",
                "allow_http_localhost",
                "network_policy",
                "proved_stateless",
            },
            "MCP HTTP transport",
        )
        transport = StreamableHttpTransportConfig(
            endpoint=_string(
                transport_raw.get("endpoint") or transport_raw.get("url") or "",
                "MCP HTTP endpoint",
            ),
            allow_http_localhost=_boolean(
                transport_raw.get("allow_http_localhost", False),
                "MCP allow_http_localhost",
            ),
            network_policy=McpHttpNetworkPolicy(
                _string(
                    transport_raw.get("network_policy", "PUBLIC_ONLY"),
                    "MCP HTTP network policy",
                ).upper()
            ),
            proved_stateless=_boolean(
                transport_raw.get("proved_stateless", False),
                "MCP proved_stateless",
            ),
        )
    else:
        raise ValueError("MCP transport type is unsupported")
    auth = _parse_auth(raw.get("auth"))
    exposure_raw = raw.get("exposure_policy", {})
    if not isinstance(exposure_raw, Mapping):
        raise ValueError("MCP exposure policy must be an object")
    _reject_unknown_keys(
        exposure_raw,
        {
            "include_tool_names",
            "include",
            "exclude_tool_names",
            "exclude",
            "invalid_tool_policy",
        },
        "MCP exposure policy",
    )
    include = exposure_raw.get("include_tool_names", exposure_raw.get("include", "ALL"))
    include_all = include is None or (
        isinstance(include, str) and include.lower() == "all"
    )
    exposure = McpExposurePolicy(
        include_tool_names=(None if include_all else _sorted_string_list(include, "MCP included tools")),
        exclude_tool_names=tuple(
            _sorted_string_list(
                exposure_raw.get(
                    "exclude_tool_names", exposure_raw.get("exclude", [])
                ),
                "MCP excluded tools",
            )
        ),
        invalid_tool_policy=McpInvalidToolPolicy(
            _string(
                exposure_raw.get("invalid_tool_policy", "FAIL_SERVER"),
                "MCP invalid tool policy",
            ).upper()
        ),
    )
    effect_raw = raw.get("effect_policy", {})
    if not isinstance(effect_raw, Mapping):
        raise ValueError("MCP effect policy must be an object")
    _reject_unknown_keys(
        effect_raw,
        {"default_effect", "tool_effect_overrides"},
        "MCP effect policy",
    )
    effect_overrides = {
        name: McpConfiguredEffect(_string(value, "MCP effect override").upper())
        for name, value in _string_mapping(
            effect_raw.get("tool_effect_overrides", {}), "MCP effect overrides"
        ).items()
    }
    effect = McpEffectPolicyConfig(
        default_effect=McpConfiguredEffect(
            _string(
                effect_raw.get("default_effect", "AUTO"),
                "MCP default effect",
            ).upper()
        ),
        tool_effect_overrides=tuple(sorted(effect_overrides.items())),
    )
    per_timeout = {
        name: _integer(value, "MCP per-tool timeout")
        for name, value in _mapping(raw.get("per_tool_timeout_ms", {}), "MCP per-tool timeout").items()
    }
    refresh_raw = raw.get("catalog_refresh_interval_ms", DEFAULT_MCP_REFRESH_INTERVAL_MS)
    refresh = (
        None
        if isinstance(refresh_raw, str) and refresh_raw.upper() == "DISABLED"
        else _integer(refresh_raw, "MCP refresh interval")
    )
    display_name = _string(raw.get("display_name") or server_id, "MCP display name")
    enabled = _boolean(raw.get("enabled", True), "MCP enabled")
    required = _boolean(raw.get("required", False), "MCP required")
    scope_policy = McpScopePolicy(
        _string(raw.get("scope_policy", "ROOT_ONLY"), "MCP scope policy").upper()
    )
    supports_parallel = _boolean(
        raw.get("supports_parallel_tool_calls", False),
        "MCP supports_parallel_tool_calls",
    )
    stateless_max = _integer(
        raw.get("stateless_http_max_in_flight", 4),
        "MCP stateless concurrency",
    )
    default_timeout = _integer(
        raw.get(
            "default_tool_timeout_ms",
            raw.get("tool_timeout_ms", DEFAULT_MCP_TOOL_TIMEOUT_MS),
        ),
        "MCP tool timeout",
    )
    per_tool_timeout = tuple(sorted(per_timeout.items()))
    semantic_fp, runtime_fp, resolved = _derive_config_fingerprints(
        server_id=server_id,
        display_name=display_name,
        enabled=enabled,
        required=required,
        transport=transport,
        auth=auth,
        exposure=exposure,
        scope_policy=scope_policy,
        effect=effect,
        supports_parallel=supports_parallel,
        stateless_http_max_in_flight=stateless_max,
        catalog_refresh_interval_ms=refresh,
        default_tool_timeout_ms=default_timeout,
        per_tool_timeout_ms=per_tool_timeout,
    )
    return McpServerConfig(
        server_id=server_id,
        display_name=display_name,
        enabled=enabled,
        required=required,
        transport=transport,
        auth=auth,
        exposure_policy=exposure,
        scope_policy=scope_policy,
        effect_policy=effect,
        supports_parallel_tool_calls=supports_parallel,
        stateless_http_max_in_flight=stateless_max,
        catalog_refresh_interval_ms=refresh,
        default_tool_timeout_ms=default_timeout,
        per_tool_timeout_ms=per_tool_timeout,
        semantic_config_fingerprint=semantic_fp,
        runtime_config_fingerprint=runtime_fp,
        resolved_config_identity=resolved,
    )


def _parse_auth(raw: object) -> McpAuthConfig:
    if raw is None:
        return NoAuth()
    if not isinstance(raw, Mapping):
        raise ValueError("MCP auth must be an object")
    _reject_unknown_keys(
        raw,
        {"type", "environment_variable", "env", "headers"},
        "MCP auth",
    )
    kind = _string(raw.get("type", "none"), "MCP auth type").lower()
    if kind in {"", "none"}:
        return NoAuth()
    if kind == "bearer_environment_ref":
        return BearerEnvironmentRef(
            _string(
                raw.get("environment_variable") or raw.get("env") or "",
                "MCP bearer environment reference",
            )
        )
    if kind == "static_header_environment_refs":
        return StaticHeaderEnvironmentRefs(
            tuple(sorted(_string_mapping(raw.get("headers", {}), "MCP auth headers").items()))
        )
    if kind == "oauth":
        return UnsupportedOAuth()
    raise ValueError("MCP auth type is unsupported")


def _transport_fingerprint_payload(value: McpTransportConfig) -> object:
    if isinstance(value, StdioTransportConfig):
        return {
            "kind": value.kind.value,
            "command": value.command,
            "args": value.args,
            "cwd": value.cwd,
            "environment": value.environment,
            "secret_environment_refs": tuple(
                (
                    target,
                    reference,
                    _secret_generation_commitment(reference),
                )
                for target, reference in value.secret_environment_refs
            ),
        }
    return {
        "kind": value.kind.value,
        "endpoint": value.endpoint,
        "allow_http_localhost": value.allow_http_localhost,
        "network_policy": value.network_policy.value,
        "proved_stateless": value.proved_stateless,
    }


def _auth_fingerprint_payload(value: McpAuthConfig) -> object:
    if isinstance(value, StaticHeaderEnvironmentRefs):
        return {
            "kind": value.kind,
            "refs": tuple(
                (name, reference, _secret_generation_commitment(reference))
                for name, reference in value.headers
            ),
        }
    if isinstance(value, BearerEnvironmentRef):
        return {
            "kind": value.kind,
            "ref": value.environment_variable,
            "secret_generation_commitment": _secret_generation_commitment(
                value.environment_variable
            ),
        }
    return {"kind": value.kind}


def _secret_generation_commitment(environment_variable: str) -> str:
    """Return one process-local opaque generation commitment, never a secret hash."""

    value = os.environ.get(environment_variable)
    payload = (
        environment_variable.encode("utf-8")
        + b"\0"
        + (value.encode("utf-8") if value is not None else b"<absent>")
    )
    return "hmac-sha256:" + hmac.new(
        _PROCESS_SECRET_COMMITMENT_KEY,
        payload,
        hashlib.sha256,
    ).hexdigest()


def _derive_config_fingerprints(
    *,
    server_id: str,
    display_name: str,
    enabled: bool,
    required: bool,
    transport: McpTransportConfig,
    auth: McpAuthConfig,
    exposure: McpExposurePolicy,
    scope_policy: McpScopePolicy,
    effect: McpEffectPolicyConfig,
    supports_parallel: bool,
    stateless_http_max_in_flight: int,
    catalog_refresh_interval_ms: int | None,
    default_tool_timeout_ms: int,
    per_tool_timeout_ms: tuple[tuple[str, int], ...],
) -> tuple[str, str, str]:
    semantic_payload = {
        "server_id": server_id,
        "display_name": display_name,
        "enabled": enabled,
        "required": required,
        "scope_policy": scope_policy.value,
        "exposure": {
            "include_tool_names": exposure.include_tool_names,
            "exclude_tool_names": exposure.exclude_tool_names,
            "invalid_tool_policy": exposure.invalid_tool_policy.value,
        },
        "effect": {
            "default_effect": effect.default_effect.value,
            "tool_effect_overrides": tuple(
                (name, value.value) for name, value in effect.tool_effect_overrides
            ),
        },
    }
    runtime_payload = {
        "transport": _transport_fingerprint_payload(transport),
        "auth": _auth_fingerprint_payload(auth),
        "supports_parallel_tool_calls": supports_parallel,
        "stateless_http_max_in_flight": stateless_http_max_in_flight,
        "catalog_refresh_interval_ms": catalog_refresh_interval_ms,
        "default_tool_timeout_ms": default_tool_timeout_ms,
        "per_tool_timeout_ms": per_tool_timeout_ms,
    }
    semantic = context_fingerprint(
        "pulsara:mcp-semantic-config:v1", semantic_payload
    )
    runtime = context_fingerprint("pulsara:mcp-runtime-config:v1", runtime_payload)
    resolved = context_fingerprint(
        "pulsara:mcp-resolved-config:v1",
        {
            "server_id": server_id,
            "semantic_config_fingerprint": semantic,
            "runtime_config_fingerprint": runtime,
        },
    )
    return semantic, runtime, resolved


def _load_raw(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError(f"MCP config path is not a file: {path}")
    data = path.read_bytes()
    if len(data) > MAXIMUM_MCP_CONFIG_BYTES:
        raise ValueError(f"MCP config exceeds the byte bound: {path}")
    text = data.decode("utf-8")
    if not text.strip():
        return {}
    payload = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"MCP config must be an object: {path}")
    if "servers" in payload:
        _reject_unknown_keys(payload, {"servers"}, "MCP config root")
    servers = payload.get("servers", payload)
    if not isinstance(servers, Mapping):
        raise ValueError(f"MCP config 'servers' must be an object: {path}")
    result: dict[str, dict[str, Any]] = {}
    if len(servers) > MAXIMUM_MCP_CONFIGURED_SERVERS:
        raise ValueError("too many configured MCP servers")
    for raw_id, raw_entry in servers.items():
        if not isinstance(raw_id, str):
            raise ValueError("MCP server id must be a string")
        server_id = raw_id.strip()
        if not server_id or not isinstance(raw_entry, Mapping):
            raise ValueError(f"MCP server entry is invalid: {raw_id!r}")
        result[server_id] = dict(raw_entry)
    return result


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be strings")
    return dict(value)  # type: ignore[arg-type]


def _reject_unknown_keys(
    value: Mapping[object, object], allowed: set[str], label: str
) -> None:
    unknown = tuple(
        sorted(str(key) for key in value if not isinstance(key, str) or key not in allowed)
    )
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {', '.join(unknown)}")


def _string_mapping(value: object, label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, item in _mapping(value, label).items():
        if not isinstance(item, str):
            raise ValueError(f"{label} values must be strings")
        result[key] = item
    return result


def _list(value: object) -> list[object]:
    if not isinstance(value, list | tuple):
        raise ValueError("MCP config field must be a list")
    return list(value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _string_list(value: object, label: str) -> tuple[str, ...]:
    items = _list(value)
    if any(not isinstance(item, str) for item in items):
        raise ValueError(f"{label} must contain strings")
    return tuple(items)  # type: ignore[arg-type]


def _sorted_string_list(value: object, label: str) -> tuple[str, ...]:
    return tuple(sorted(_string_list(value, label)))


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _validate_names(values: tuple[str, ...], label: str) -> None:
    if values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise ValueError(f"{label} must be sorted and unique")
    if any(not value or len(value.encode("utf-8")) > 256 for value in values):
        raise ValueError(f"{label} contains an invalid name")


def _validate_unique_pairs(values: tuple[tuple[str, str], ...], label: str) -> None:
    names = tuple(name for name, _ in values)
    if names != tuple(sorted(names)) or len(names) != len(set(names)):
        raise ValueError(f"{label} must be sorted and unique")
    if any(not name or "\x00" in name or "\x00" in value for name, value in values):
        raise ValueError(f"{label} contains an invalid value")


def _valid_env_name(value: str) -> bool:
    return bool(value) and value.replace("_", "a").isalnum() and not value[0].isdigit()


__all__ = [
    "BearerEnvironmentRef",
    "DEFAULT_USER_MCP_CONFIG",
    "DetectedMcpServerConfig",
    "McpConfiguredEffect",
    "McpEffectPolicyConfig",
    "McpExposurePolicy",
    "McpInvalidToolPolicy",
    "McpHttpNetworkPolicy",
    "McpScopePolicy",
    "McpServerConfig",
    "McpTransportKind",
    "NoAuth",
    "StaticHeaderEnvironmentRefs",
    "StdioTransportConfig",
    "StreamableHttpTransportConfig",
    "UnsupportedOAuth",
    "WORKSPACE_MCP_CONFIG",
    "load_mcp_server_configs",
    "set_mcp_server_enabled",
    "write_mcp_server_config",
]
