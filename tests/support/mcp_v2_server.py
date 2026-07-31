"""Small real SDK v2 server used by transport-level contract tests."""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy

from mcp.server import MCPServer
from mcp.server.mcpserver.tools import Tool


def echo_region(region: str, payload: str = "ok") -> str:
    """Echo a primitive argument that must also travel in Mcp-Param-Region."""

    return f"{region}:{payload}"


def build_server() -> MCPServer:
    tool = Tool.from_function(echo_region, structured_output=False)
    parameters = deepcopy(tool.parameters)
    parameters["properties"]["region"]["x-mcp-header"] = "Region"
    tool = tool.model_copy(update={"parameters": parameters})
    return MCPServer(
        name="pulsara-mcp-v2-test-server",
        version="1.0.0",
        tools=[tool],
        log_level="ERROR",
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("stdio", "http"))
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = build_server()
    if args.mode == "stdio":
        await server.run_stdio_async()
        return
    if args.port <= 0:
        raise ValueError("HTTP mode requires --port")
    await server.run_streamable_http_async(
        host="127.0.0.1",
        port=args.port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
