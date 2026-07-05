import asyncio
import os

import pytest

from pulsara_agent.runtime.mcp import McpServerConfig, McpServerStatus, McpStreamableHttpConfig, SdkMcpClientManager


pytestmark = pytest.mark.skipif(
    os.getenv("PULSARA_RUN_REAL_MCP") != "1",
    reason="Set PULSARA_RUN_REAL_MCP=1 to run real remote MCP dogfood tests.",
)


def test_real_langchain_docs_mcp_dogfood() -> None:
    """Exercise a public, no-key streamable HTTP MCP server through the SDK-backed manager."""

    async def run() -> None:
        config = McpServerConfig(
            server_id="langchain-docs",
            transport=McpStreamableHttpConfig(url="https://docs.langchain.com/mcp"),
            startup_timeout_ms=20_000,
            tool_timeout_ms=20_000,
        )
        manager = await SdkMcpClientManager.start((config,))
        try:
            snapshot = manager.snapshots[0]
            assert snapshot.status is McpServerStatus.READY
            tool_names = {tool.name for tool in snapshot.tools}
            assert "search_docs_by_lang_chain" in tool_names

            result = await manager.call_tool(
                "langchain-docs",
                "search_docs_by_lang_chain",
                {"query": "LangChain MCP adapters Python quickstart"},
                timeout_ms=20_000,
            )

            assert result.is_error is False
            assert "LangChain" in result.output
        finally:
            await manager.aclose(timeout_seconds=2)

    asyncio.run(run())
