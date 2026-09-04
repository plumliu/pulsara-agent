"""Normalize one frozen Plugin MCP observation into the native MCP owner."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
from typing import Iterable

from pulsara_agent.mcp_config import (
    DEFAULT_MCP_REFRESH_INTERVAL_MS,
    DEFAULT_MCP_TOOL_TIMEOUT_MS,
    MAXIMUM_MCP_CONFIGURED_SERVERS,
    ExactAbsoluteMcpCwd,
    ManagedPackageMcpRuntimeSource,
    McpAbsoluteCwdAuthority,
    McpConfiguredEffect,
    McpEffectPolicyConfig,
    McpExposurePolicy,
    McpHttpNetworkPolicy,
    McpScopePolicy,
    McpServerConfig,
    NoAuth,
    StdioTransportConfig,
    StreamableHttpTransportConfig,
    freeze_mcp_server_config,
)
from pulsara_agent.plugins.contracts import (
    EnabledPluginViewDisposition,
    PluginComponentObservationDisposition,
    PluginDiagnostic,
    PluginDiagnosticCode,
    PluginMcpNormalizationDisposition,
    PluginMcpHttpSummary,
    PluginMcpStdioSummary,
    PluginScopeKind,
)
from pulsara_agent.plugins.package_store import PhysicalLifetimeAnchor
from pulsara_agent.plugins.view import (
    FrozenEnabledPluginInstance,
    FrozenEnabledPluginView,
)


_PLACEHOLDER = re.compile(r"\$\{(?:PLUGIN_ROOT|PLUGIN_DATA)\}")


@dataclass(frozen=True, slots=True)
class PluginMcpNormalizationResult:
    """One complete call-local merge candidate for the native supervisor."""

    configs: tuple[McpServerConfig, ...]
    plugin_configs: tuple[McpServerConfig, ...]
    diagnostics: tuple[PluginDiagnostic, ...]
    disposition: PluginMcpNormalizationDisposition = (
        PluginMcpNormalizationDisposition.COMPLETE
    )
    configured_bound_exceeded: bool = False

    def __post_init__(self) -> None:
        if (
            self.disposition is PluginMcpNormalizationDisposition.UNAVAILABLE
            and self.plugin_configs
        ):
            raise ValueError("unavailable Plugin MCP normalization is partial")

    def close_plugin_anchors(self) -> None:
        for config in self.plugin_configs:
            _close_anchor(config.physical_lifetime_anchor)


def normalize_plugin_mcp_configs(
    *,
    existing_configs: tuple[McpServerConfig, ...],
    view: FrozenEnabledPluginView,
) -> PluginMcpNormalizationResult:
    """Build the one deterministic local+Plugin native config tuple.

    Component selection consumes the exact parsed objects in ``view``.  It
    neither reparses package files nor reads lifecycle state.
    """

    existing_ids = {item.server_id for item in existing_configs}
    if len(existing_ids) != len(existing_configs):
        raise ValueError("existing MCP server ids are not unique")
    if view.disposition is EnabledPluginViewDisposition.UNAVAILABLE:
        return PluginMcpNormalizationResult(
            tuple(sorted(existing_configs, key=lambda item: item.server_id)),
            (),
            (_diagnostic(PluginDiagnosticCode.VIEW_UNAVAILABLE),),
            PluginMcpNormalizationDisposition.UNAVAILABLE,
        )

    diagnostics: list[PluginDiagnostic] = []
    candidates: list[McpServerConfig] = []
    for instance, server in _selected_servers(view):
        try:
            config = _normalize_server(instance, server)
        except ValueError:
            diagnostics.append(
                _diagnostic(
                    PluginDiagnosticCode.MCP_SERVER_INVALID,
                    component=f"{instance.identity.plugin_id}:{server.local_server_id}",
                )
            )
            continue
        except (MemoryError, OSError):
            for item in candidates:
                _close_anchor(item.physical_lifetime_anchor)
            diagnostics.append(_diagnostic(PluginDiagnosticCode.VIEW_UNAVAILABLE))
            return PluginMcpNormalizationResult(
                tuple(sorted(existing_configs, key=lambda item: item.server_id)),
                (),
                tuple(diagnostics),
                PluginMcpNormalizationDisposition.UNAVAILABLE,
            )
        candidates.append(config)

    grouped: dict[str, list[McpServerConfig]] = {}
    for config in candidates:
        grouped.setdefault(config.server_id, []).append(config)
    accepted: list[McpServerConfig] = []
    for server_id in sorted(grouped):
        group = grouped[server_id]
        if server_id in existing_ids or len(group) != 1:
            for item in group:
                _close_anchor(item.physical_lifetime_anchor)
            diagnostics.append(
                _diagnostic(
                    PluginDiagnosticCode.MCP_SERVER_ID_COLLISION,
                    component=server_id,
                )
            )
            continue
        accepted.append(group[0])

    merged = tuple(
        sorted((*existing_configs, *accepted), key=lambda item: item.server_id)
    )
    if len(merged) > MAXIMUM_MCP_CONFIGURED_SERVERS:
        for item in accepted:
            _close_anchor(item.physical_lifetime_anchor)
        diagnostics.append(
            _diagnostic(PluginDiagnosticCode.MCP_CONFIGURED_BOUND_EXCEEDED)
        )
        return PluginMcpNormalizationResult(
            tuple(sorted(existing_configs, key=lambda item: item.server_id)),
            (),
            tuple(diagnostics),
            configured_bound_exceeded=True,
        )
    return PluginMcpNormalizationResult(
        merged,
        tuple(accepted),
        tuple(diagnostics),
    )


def framed_plugin_mcp_server_id(plugin_id: str, local_server_id: str) -> str:
    value = f"plugin:{len(plugin_id.encode('utf-8'))}:{plugin_id}:{local_server_id}"
    if len(value.encode("utf-8")) > 128:
        raise ValueError("Plugin MCP server id exceeds the native bound")
    return value


def _selected_servers(
    view: FrozenEnabledPluginView,
) -> Iterable[
    tuple[FrozenEnabledPluginInstance, PluginMcpStdioSummary | PluginMcpHttpSummary]
]:
    user = {item.identity.plugin_id: item for item in view.user_instances}
    workspace = {item.identity.plugin_id: item for item in view.workspace_instances}
    for plugin_id in sorted(set(user) | set(workspace)):
        user_instance = user.get(plugin_id)
        workspace_instance = workspace.get(plugin_id)
        if workspace_instance is None:
            yield from _valid_servers(user_instance)
            continue
        workspace_component = workspace_instance.mcp
        if workspace_component.disposition in {
            PluginComponentObservationDisposition.INVALID,
            PluginComponentObservationDisposition.UNAVAILABLE,
        }:
            # The higher-scope set is unknown, so it owns the whole component
            # boundary and no lower-scope process is guessed into existence.
            continue
        if (
            workspace_component.disposition
            is PluginComponentObservationDisposition.MISSING
        ):
            yield from _valid_servers(user_instance)
            continue
        assert workspace_component.parsed is not None
        claimed = frozenset(workspace_component.parsed.declared_server_ids)
        yield from _valid_servers(workspace_instance)
        if user_instance is not None and user_instance.mcp.parsed is not None:
            for server in user_instance.mcp.parsed.valid_servers:
                if server.local_server_id not in claimed:
                    yield user_instance, server


def _valid_servers(
    instance: FrozenEnabledPluginInstance | None,
) -> Iterable[
    tuple[FrozenEnabledPluginInstance, PluginMcpStdioSummary | PluginMcpHttpSummary]
]:
    if instance is None or instance.mcp.parsed is None:
        return
    for server in instance.mcp.parsed.valid_servers:
        if isinstance(server, (PluginMcpStdioSummary, PluginMcpHttpSummary)):
            yield instance, server


def _normalize_server(
    instance: FrozenEnabledPluginInstance,
    server: PluginMcpStdioSummary | PluginMcpHttpSummary,
) -> McpServerConfig:
    plugin_id = instance.identity.plugin_id
    server_id = framed_plugin_mcp_server_id(plugin_id, server.local_server_id)
    anchor = instance.physical_lifetime_anchor.duplicate()
    try:
        package_root = instance.package_root
        data_root = instance.data_root
        if isinstance(server, PluginMcpStdioSummary):
            command = server.command
            if command.startswith("./"):
                command = str(_contained(package_root, command[2:]))
            environment = {
                key: _expand(value, package_root=package_root, data_root=data_root)
                for key, value in server.environment
            }
            if any(key in environment for key in ("PLUGIN_ROOT", "PLUGIN_DATA")):
                raise ValueError("Plugin MCP environment uses a reserved key")
            environment["PLUGIN_ROOT"] = str(package_root)
            environment["PLUGIN_DATA"] = str(data_root)
            cwd, authority = _resolve_cwd(
                server.cwd, package_root=package_root, data_root=data_root
            )
            transport = StdioTransportConfig(
                command=command,
                args=tuple(
                    _expand(value, package_root=package_root, data_root=data_root)
                    for value in server.args
                ),
                cwd=ExactAbsoluteMcpCwd(cwd, authority),
                environment=tuple(sorted(environment.items())),
                lookup_path=os.environ.get("PATH", ""),
            )
        else:
            transport = StreamableHttpTransportConfig(
                endpoint=server.endpoint,
                allow_http_localhost=server.endpoint.lower().startswith("http://"),
                network_policy=McpHttpNetworkPolicy.PUBLIC_ONLY,
            )
        source = ManagedPackageMcpRuntimeSource(
            store_scope_key=(
                "user"
                if instance.identity.scope is PluginScopeKind.USER
                else f"workspace:{instance.identity.workspace_state_key}"
            ),
            package_owner_key=plugin_id,
            package_install_id=instance.state.current_package_install_id,
        )
        return freeze_mcp_server_config(
            server_id=server_id,
            display_name=f"{plugin_id}:{server.local_server_id}",
            enabled=True,
            required=False,
            transport=transport,
            auth=NoAuth(),
            exposure_policy=McpExposurePolicy(),
            scope_policy=McpScopePolicy.ROOT_AND_SUBAGENTS,
            effect_policy=McpEffectPolicyConfig(
                default_effect=McpConfiguredEffect.AUTO
            ),
            supports_parallel_tool_calls=False,
            stateless_http_max_in_flight=1,
            catalog_refresh_interval_ms=DEFAULT_MCP_REFRESH_INTERVAL_MS,
            default_tool_timeout_ms=DEFAULT_MCP_TOOL_TIMEOUT_MS,
            per_tool_timeout_ms=(),
            runtime_source=source,
            public_headers=(
                server.public_headers
                if isinstance(server, PluginMcpHttpSummary)
                else ()
            ),
            physical_lifetime_anchor=anchor,
        )
    except BaseException:
        anchor.close()
        raise


def _resolve_cwd(
    value: str | None, *, package_root: Path, data_root: Path
) -> tuple[Path, McpAbsoluteCwdAuthority]:
    if value is None:
        return package_root, McpAbsoluteCwdAuthority.PACKAGE_ROOT
    if value.startswith("./"):
        return (
            _contained(package_root, value[2:]),
            McpAbsoluteCwdAuthority.PACKAGE_ROOT,
        )
    if value.startswith("${PLUGIN_ROOT}"):
        suffix = value[len("${PLUGIN_ROOT}") :].lstrip("/")
        return (
            _contained(package_root, suffix),
            McpAbsoluteCwdAuthority.PACKAGE_ROOT,
        )
    if value.startswith("${PLUGIN_DATA}"):
        suffix = value[len("${PLUGIN_DATA}") :].lstrip("/")
        return (
            _contained(data_root, suffix),
            McpAbsoluteCwdAuthority.INSTANCE_DATA,
        )
    raise ValueError("Plugin MCP cwd has no typed authority")


def _contained(root: Path, suffix: str) -> Path:
    relative = PurePosixPath(suffix)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError("Plugin MCP path escapes its authority")
    result = root.joinpath(*relative.parts)
    result.relative_to(root)
    return result


def _expand(value: str, *, package_root: Path, data_root: Path) -> str:
    replacements = {
        "${PLUGIN_ROOT}": str(package_root),
        "${PLUGIN_DATA}": str(data_root),
    }
    return _PLACEHOLDER.sub(lambda match: replacements[match.group(0)], value)


def _close_anchor(value: object | None) -> None:
    if isinstance(value, PhysicalLifetimeAnchor):
        value.close()


def _diagnostic(
    code: PluginDiagnosticCode, *, component: str | None = None
) -> PluginDiagnostic:
    return PluginDiagnostic(
        code,
        code.value.removeprefix("plugin_").replace("_", " "),
        component=component,
    )


__all__ = [
    "PluginMcpNormalizationResult",
    "framed_plugin_mcp_server_id",
    "normalize_plugin_mcp_configs",
]
