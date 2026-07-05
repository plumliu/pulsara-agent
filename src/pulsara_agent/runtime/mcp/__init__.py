"""MCP runtime support."""

from pulsara_agent.runtime.mcp.manager import CompositeMcpClientManager, McpClientManager, MockMcpClientManager
from pulsara_agent.runtime.mcp.client import HttpMcpClientManager
from pulsara_agent.runtime.mcp.sdk import SdkMcpClientManager
from pulsara_agent.runtime.mcp.stdio import StdioMcpClientManager
from pulsara_agent.runtime.mcp.types import (
    McpContentArtifact,
    McpDiscoveredTool,
    McpDiscoveredPrompt,
    McpDiscoveredResource,
    McpDiscoveredResourceTemplate,
    McpInputRequestDTO,
    McpInputRequired,
    McpInputRequiredResolution,
    McpOriginalRequest,
    McpRequestSourceMethod,
    McpServerConfig,
    McpServerSnapshot,
    McpServerStatus,
    McpServerTransportKind,
    McpStdioConfig,
    McpStreamableHttpConfig,
    McpToolAnnotations,
    McpToolResult,
    mangle_mcp_tool_name,
)

__all__ = [
    "CompositeMcpClientManager",
    "McpContentArtifact",
    "McpClientManager",
    "McpDiscoveredPrompt",
    "McpDiscoveredResource",
    "McpDiscoveredResourceTemplate",
    "McpDiscoveredTool",
    "McpInputRequestDTO",
    "McpInputRequired",
    "McpInputRequiredResolution",
    "McpOriginalRequest",
    "McpRequestSourceMethod",
    "McpServerConfig",
    "McpServerSnapshot",
    "McpServerStatus",
    "McpServerTransportKind",
    "McpStdioConfig",
    "McpStreamableHttpConfig",
    "McpToolAnnotations",
    "McpToolResult",
    "MockMcpClientManager",
    "HttpMcpClientManager",
    "SdkMcpClientManager",
    "StdioMcpClientManager",
    "mangle_mcp_tool_name",
]
