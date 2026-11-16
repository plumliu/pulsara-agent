"""Construct current model Targets from the real fixture owner layouts."""

from pulsara_agent.capability.mcp_management import LocalMcpTarget
from pulsara_agent.plugins.contracts import PluginScopeKind


def plugin_target(service, name, *, scope="USER"):
    store = service.plugins._store()
    identity = store.identity(
        scope=PluginScopeKind(scope),
        plugin_id=name,
        workspace_root=service.workspace_root if scope == "WORKSPACE" else None,
    )
    return {
        "kind": "PLUGIN",
        "plugin_path": str(store.layout(identity).plugin_package_parent),
    }


def mcp_target(service, name, *, scope="USER", plugin=None):
    if plugin is None:
        path = service.mcp.path(
            LocalMcpTarget(
                name, service.workspace_root if scope == "WORKSPACE" else None
            )
        )
    else:
        store = service.plugins._store()
        identity = store.identity(
            scope=PluginScopeKind(scope),
            plugin_id=plugin,
            workspace_root=service.workspace_root if scope == "WORKSPACE" else None,
        )
        layout = store.layout(identity)
        state = store.read_state(layout)
        if state is None:
            raise ValueError("fixture Plugin is not installed")
        path = (
            layout.plugin_package_parent / state.current_package_install_id / "mcp.json"
        )
    return {
        "kind": "MCP_CONNECTION",
        "config_path": str(path.resolve()),
        "server_id": name,
    }


def hook_target(service, *, scope="USER", plugin=None):
    if plugin is None:
        path = (
            service.hooks.user_config_path
            if scope == "USER"
            else service.workspace_root / ".pulsara/hooks.json"
        )
    else:
        connection = mcp_target(service, "unused", scope=scope, plugin=plugin)
        from pathlib import Path

        path = Path(connection["config_path"]).parent / "dev.pulsara/hooks/hooks.json"
    return {"kind": "HOOK_SOURCE", "config_path": str(path.resolve())}
