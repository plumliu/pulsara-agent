"""Real public MCP SDK peer; only fixture tool behavior creates Windows work."""

import asyncio
import json
import os
from pathlib import Path
import sys
import time

from mcp.server.mcpserver import MCPServer
import mcp_types as types

configuration = json.loads(Path(sys.argv[1]).read_text())
server = MCPServer("pulsara-g0-mcp-stdio")


def log(event, **facts):
    with open(configuration["server_record"], "a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                dict(
                    event=event,
                    time=time.time(),
                    pid=os.getpid(),
                    starttime=Path("/proc/self/stat")
                    .read_text()
                    .rsplit(")", 1)[1]
                    .split()[19],
                    **facts,
                )
            )
            + "\n"
        )


@server.tool(
    annotations=types.ToolAnnotations(
        read_only_hint=False, destructive_hint=False, open_world_hint=True
    )
)
async def spawn_tail(mode: str) -> str:
    """Start finite ordinary Windows work, retaining no Windows cleanup wrapper."""
    log("tool-entered", mode=mode)
    process = await asyncio.create_subprocess_exec(
        *configuration["task_args"], stdin=asyncio.subprocess.DEVNULL
    )
    log("tool-created-interop-child", childPid=process.pid)
    try:
        if mode == "normal":
            code = await process.wait()
            log("tool-normal-reply", exit=code)
            return f"MCP-ACK:héllo:{code}"
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        log("sdk-tool-cancel-notification-observed")
        raise
    finally:
        # The generic MCP peer has no per-call Windows ownership API. The G0
        # fixture deliberately leaves ordinary external work to session scope.
        log("tool-handler-left", linuxChildReturncode=process.returncode)


if __name__ == "__main__":
    log("sdk-server-started")
    server.run("stdio")
