"""MCP compatibility facade with no eager runtime composition."""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "McpClientManager": ("pulsara_agent.runtime.mcp.manager", "McpClientManager"),
    "SdkMcpClientManager": (
        "pulsara_agent.runtime.mcp.sdk",
        "SdkMcpClientManager",
    ),
    "SdkMcpConnection": ("pulsara_agent.runtime.mcp.sdk", "SdkMcpConnection"),
    "discover_mcp_server": (
        "pulsara_agent.runtime.mcp.sdk",
        "discover_mcp_server",
    ),
    "McpServerSupervisor": (
        "pulsara_agent.runtime.mcp.supervisor",
        "McpServerSupervisor",
    ),
}
for _name in (
    "McpBindingIdentity",
    "McpContentArtifact",
    "McpDiscoveredTool",
    "McpDiscoveredPrompt",
    "McpDiscoveredResource",
    "McpDiscoveredResourceTemplate",
    "McpRequiredStartupError",
    "McpServerConfig",
    "McpServerSnapshot",
    "McpServerStatus",
    "McpServerTransportKind",
    "McpStdioConfig",
    "McpStreamableHttpConfig",
    "McpToolAnnotations",
    "McpToolResult",
    "McpInstalledCapabilitySnapshot",
    "mangle_mcp_tool_name",
):
    _EXPORTS[_name] = ("pulsara_agent.runtime.mcp.types", _name)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

__all__ = [
    "McpBindingIdentity",
    "McpContentArtifact",
    "McpClientManager",
    "McpDiscoveredPrompt",
    "McpDiscoveredResource",
    "McpDiscoveredResourceTemplate",
    "McpDiscoveredTool",
    "McpRequiredStartupError",
    "McpServerConfig",
    "McpServerSnapshot",
    "McpServerStatus",
    "McpServerTransportKind",
    "McpStdioConfig",
    "McpStreamableHttpConfig",
    "McpToolAnnotations",
    "McpToolResult",
    "McpInstalledCapabilitySnapshot",
    "McpServerSupervisor",
    "SdkMcpClientManager",
    "SdkMcpConnection",
    "discover_mcp_server",
    "mangle_mcp_tool_name",
]
