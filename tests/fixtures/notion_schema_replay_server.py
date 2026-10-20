"""Replay recorded Notion schemas through the public MCP SDK; never access Notion."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
import mcp_types as types

tools = json.loads(
    Path(__file__).with_name("notion_native_tools_20261020.json").read_text()
)["tools"]


async def list_tools(_ctx, _params) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(
                **tool,
                annotations=types.ToolAnnotations(
                    readOnlyHint=True, destructiveHint=False, openWorldHint=False
                ),
            )
            for tool in tools
        ]
    )


async def call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    if params.name != "notion_get_self" or params.arguments:
        raise ValueError("Only the read-only schema replay probe can be called")
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="NOTION_SCHEMA_REPLAY_OK")]
    )


server = Server(
    "notion-schema-replay", on_list_tools=list_tools, on_call_tool=call_tool
)


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
