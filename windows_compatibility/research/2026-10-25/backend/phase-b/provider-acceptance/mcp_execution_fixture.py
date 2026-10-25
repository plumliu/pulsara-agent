"""Public SDK fixture extension; no copied MCP protocol or external service."""
import argparse
import asyncio
import base64
import importlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import mcp_types as types

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=("stdio", "http"))
parser.add_argument("journal", type=Path)
parser.add_argument("--socket-fd", type=int)
args = parser.parse_args()
sys.path.insert(0, str(Path(__file__).parent / "tests/fixtures"))
fixture = importlib.import_module("round6_mcp_server")
server, fixture_echo = fixture.server, fixture.fixture_echo


@server.tool(name="execution_echo", annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
def execution_echo(text: str) -> str:
    """Echo one explicit test marker and record this controlled fixture call."""
    row = {"transport": args.mode, "text": text}
    if args.mode == "stdio":
        pe = subprocess.run(["/mnt/c/Windows/System32/cmd.exe", "/d", "/c", "echo", "MCP_STDIO_PE_DONE"], capture_output=True, check=True)
        row["windows_pe"] = pe.stdout.decode("utf-8", errors="replace").strip()
        row["stdout_b64"] = base64.b64encode(pe.stdout).decode("ascii")
        row["stderr_b64"] = base64.b64encode(pe.stderr).decode("ascii")
        assert b"MCP_STDIO_PE_DONE" in pe.stdout
    with args.journal.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row) + "\n")
    return fixture_echo(text)


if args.mode == "stdio":
    server.run("stdio")
else:
    import uvicorn

    async def main():
        listener = socket.socket(fileno=args.socket_fd)
        app = server.streamable_http_app(stateless_http=True, host="127.0.0.1")
        owner = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        task = asyncio.create_task(owner.serve(sockets=[listener]))
        while not owner.started:
            if task.done():
                await task
                raise RuntimeError("HTTP fixture stopped before ready")
            await asyncio.sleep(0)
        print(json.dumps({"fixture_ready": True}), flush=True)
        async def stop_on_eof():
            reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(reader)
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
            try:
                await reader.read()
                owner.should_exit = True
            finally:
                transport.close()
        stopping = asyncio.create_task(stop_on_eof())
        await task
        if not stopping.done():
            stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)

    asyncio.run(main())
