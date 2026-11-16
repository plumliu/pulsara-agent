"""Resolve public path Targets through the existing capability source owners."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pulsara_agent.mcp_config import (workspace_mcp_config_path, LocalConfiguredMcpRuntimeSource, ManagedPackageMcpRuntimeSource)
from pulsara_agent.plugins.contracts import PluginScopeKind, PluginInstanceIdentity


def exact_target_path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    path = Path(value)
    if ".." in path.parts or path != path.resolve(strict=False):
        raise ValueError(f"{field} must be a canonical absolute path")
    return path


def resolve_capability_target(service, target: Mapping[str, object]) -> dict[str, object]:
    """Produce native owner selectors, not another public Target or permission."""
    kind = target.get("kind")
    if kind not in {"PLUGIN", "HOOK_SOURCE", "MCP_CONNECTION"}:
        raise ValueError("unsupported source Target")
    field = "plugin_path" if kind == "PLUGIN" else "config_path"
    path = exact_target_path(target.get(field), field)
    root = service.workspace_root if service.workspace_kind == "project" else None
    if kind == "HOOK_SOURCE":
        if path == service.hooks.user_config_path.resolve(strict=False):
            return {"scope": "USER", "source_kind": "LOCAL"}
        if root is not None and path == (root / ".pulsara/hooks.json").resolve(strict=False):
            return {"scope": "WORKSPACE", "source_kind": "LOCAL"}
    elif kind == "MCP_CONNECTION":
        if path == service.mcp.user_config_path.resolve(strict=False):
            return {"scope": "USER", "source_kind": "LOCAL", "server_id": target["server_id"]}
        if root is not None and path == workspace_mcp_config_path(root).resolve(strict=False):
            return {"scope": "WORKSPACE", "source_kind": "LOCAL", "server_id": target["server_id"]}
    store = service.plugins._store()
    if store is None:
        raise ValueError("Plugin store is unavailable")
    if kind == "PLUGIN":
        for scope, workspace in ((PluginScopeKind.USER, None), (PluginScopeKind.WORKSPACE, root)):
            if scope is PluginScopeKind.WORKSPACE and workspace is None:
                continue
            identity = store.identity(scope=scope, plugin_id=path.name, workspace_root=workspace)
            if store.layout(identity).plugin_package_parent == path:
                return {"scope": scope.value, "plugin_id": identity.plugin_id}
        raise ValueError("Plugin Target is outside the current installation sources")
    identity = store.current_package_identity_for_path(path, workspace_root=root)
    if identity is None:
        raise ValueError("The configuration does not belong to a current Plugin installation")
    state = store.read_state(store.layout(identity))
    if state is None:
        raise ValueError("Plugin is no longer installed")
    package = store.layout(identity).plugin_package_parent / state.current_package_install_id
    expected = package / ("mcp.json" if kind == "MCP_CONNECTION" else "dev.pulsara/hooks/hooks.json")
    if path != expected:
        raise ValueError("Target is not the Plugin's declared configuration")
    result = {"scope": identity.scope.value, "source_kind": "PLUGIN", "plugin_id": identity.plugin_id}
    if kind == "MCP_CONNECTION":
        result["server_id"] = target["server_id"]
    return result


def plugin_target_from_inspection(item) -> dict[str, str]:
    return {"kind": "PLUGIN", "plugin_path": str(item.package_root.parent)}


def mcp_connection_target(*, server_id: str, runtime_source, workspace_root: Path, pulsara_home: Path, user_config_path: Path):
    """Project the source owner's exact layout; HOST_OVERRIDE has no public Target."""
    if isinstance(runtime_source, LocalConfiguredMcpRuntimeSource):
        if runtime_source.source_kind.value == "USER":
            path = user_config_path
        elif runtime_source.source_kind.value == "WORKSPACE":
            path = workspace_mcp_config_path(workspace_root)
        else:
            return None
        return {"kind": "MCP_CONNECTION", "config_path": str(path), "server_id": server_id}
    if isinstance(runtime_source, ManagedPackageMcpRuntimeSource):
        from pulsara_agent.plugins.package_store import PluginStoreLayout
        from pulsara_agent.plugins.mcp_adapter import framed_plugin_mcp_server_id

        prefix = framed_plugin_mcp_server_id(runtime_source.package_owner_key, "")
        if not server_id.startswith(prefix) or not server_id[len(prefix):]:
            raise ValueError("Plugin MCP server identity does not join its source")
        scope_key = runtime_source.store_scope_key
        identity = PluginInstanceIdentity(PluginScopeKind.USER, runtime_source.package_owner_key) if scope_key == "user" else PluginInstanceIdentity(
            PluginScopeKind.WORKSPACE, runtime_source.package_owner_key, scope_key.removeprefix("workspace:"))
        path = (PluginStoreLayout(pulsara_home, identity).plugin_package_parent
                / runtime_source.package_install_id / "mcp.json")
        return {"kind": "MCP_CONNECTION", "config_path": str(path), "server_id": server_id[len(prefix):]}
    raise TypeError("MCP source union is open")


__all__ = ["exact_target_path", "resolve_capability_target", "plugin_target_from_inspection", "mcp_connection_target"]
