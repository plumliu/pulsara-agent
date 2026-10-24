"""Research-only opaque relay. Gates count bytes; no WSL message parsing."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile


async def main():
    upstream, record, root, mode, gate = sys.argv[1:]
    output = open(record, "a", buffering=1)

    def log(event, **facts):
        output.write(json.dumps(dict(event=event, pid=os.getpid(), **facts)) + "\n")

    log(
        "before-pin",
        upstream=upstream,
        starttime=Path("/proc/self/stat").read_text().split()[21],
    )
    try:
        fd = os.open(upstream, os.O_PATH | os.O_CLOEXEC)
    except OSError as error:
        log("pin-failed", errno=error.errno, message=str(error))
        return
    directory = tempfile.mkdtemp(prefix="relay-", dir=root)
    path = directory + "/interop"
    pinned = f"/proc/{os.getpid()}/fd/{fd}"

    async def pause(event, **facts):
        log(event, **facts)
        while not Path(gate).exists():
            await asyncio.sleep(0.01)

    async def connected(reader, writer):
        remote = None
        try:
            other, remote = await asyncio.open_unix_connection(pinned)

            async def forward():
                first = True
                total = 0
                while True:
                    chunk = await reader.read(1 if first and mode == "half" else 65536)
                    if not chunk:
                        break
                    if first and mode == "before":
                        await pause("before-forward", bytes=len(chunk))
                    remote.write(chunk)
                    await remote.drain()
                    total += len(chunk)
                    log("forwarded", bytes=len(chunk), total=total)
                    if first and mode == "half":
                        await pause("half-forwarded", total=total)
                    if first and mode == "after":
                        await pause("after-forwarded", total=total)
                    first = False
                if remote.can_write_eof():
                    remote.write_eof()

            async def reply():
                total = 0
                while True:
                    chunk = await other.read(65536)
                    if not chunk:
                        break
                    if mode == "reply" and total == 0:
                        await pause("reply-blocked", bytes=len(chunk))
                    writer.write(chunk)
                    await writer.drain()
                    total += len(chunk)
                    log("replied", bytes=len(chunk), total=total)
                if writer.can_write_eof():
                    writer.write_eof()

            await asyncio.gather(forward(), reply())
            log("connection-complete")
        except Exception as error:
            log("connection-error", kind=type(error).__name__, message=str(error))
        finally:
            for stream in (remote, writer):
                if stream:
                    stream.close()

    server = await asyncio.start_unix_server(connected, path=path)
    log(
        "relay-ready",
        socket=path,
        directory=directory,
        pinned=pinned,
        inode=os.stat(pinned).st_ino,
        mode=oct(os.stat(directory).st_mode & 0o777),
        starttime=Path("/proc/self/stat").read_text().split()[21],
    )

    async def seal():
        while not Path(gate + ".seal").exists():
            await asyncio.sleep(0.01)
        server.close()
        log("admission-sealed", socketStillExists=os.path.exists(path))
        await server.wait_closed()

    asyncio.create_task(seal())
    async with server:
        await asyncio.Event().wait()


asyncio.run(main())
