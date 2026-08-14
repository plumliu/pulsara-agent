"""Small public-SDK MCP server used by Round 6 transport tests."""

from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer
import mcp_types as types


server = MCPServer(
    "pulsara-round6-fixture",
    instructions="Untrusted fixture instructions.",
)
_parallel_probe_active = 0
_parallel_probe_ready = asyncio.Event()


@server.tool(
    annotations=types.ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    )
)
def fixture_echo(text: str) -> str:
    """Return one bounded input string."""

    return f"fixture:{text}"


@server.tool(
    annotations=types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        openWorldHint=True,
    )
)
def fixture_effect(value: str) -> str:
    """Represent a deterministic external-effect-class tool."""

    return f"effect:{value}"


@server.tool(
    annotations=types.ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    )
)
async def fixture_delay(delay_ms: int) -> str:
    """Sleep for a bounded test interval and return its value."""

    if not 1 <= delay_ms <= 1_000:
        raise ValueError("delay is out of bounds")
    await asyncio.sleep(delay_ms / 1_000)
    return f"delayed:{delay_ms}"


@server.tool(
    annotations=types.ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    )
)
async def fixture_parallel_probe() -> str:
    """Report whether a second request entered this server concurrently."""

    global _parallel_probe_active
    _parallel_probe_active += 1
    if _parallel_probe_active >= 2:
        _parallel_probe_ready.set()
    try:
        try:
            await asyncio.wait_for(_parallel_probe_ready.wait(), timeout=0.25)
            return "parallel:yes"
        except TimeoutError:
            return "parallel:no"
    finally:
        _parallel_probe_active -= 1
        if _parallel_probe_active == 0:
            _parallel_probe_ready.clear()


@server.resource(
    "fixture://round6/resource",
    name="round6-resource",
    description="One untrusted resource.",
    mime_type="text/plain",
)
def fixture_resource() -> str:
    return "round6 resource body"


@server.prompt(name="round6_prompt", description="One untrusted prompt.")
def fixture_prompt(topic: str = "default") -> str:
    return f"Discuss {topic}."


if __name__ == "__main__":
    server.run("stdio")
