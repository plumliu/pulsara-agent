"""Deterministic SDK scheduling fault: cancel a connection before its first step."""

import array
import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
from relay_fixture import Fixture, identity, rows


async def main():
    root = Path(tempfile.mkdtemp(prefix=".g0r4check-", dir="/home/plumwsl"))
    cfg = dict(
        directory=str(root / "scope"),
        record=str(root / "relay.jsonl"),
        anchor_record=str(root / "anchor.jsonl"),
        prefix=str(root / "commands"),
    )
    fixture = Fixture(cfg, "receiver")
    connected = fixture.connected

    def cancel_before_task_step(reader, writer):
        connected(reader, writer)
        for task in tuple(fixture.tasks):
            task.cancel()
        fixture.file(".cancel").touch()

    fixture.connected = cancel_before_task_step
    process = asyncio.create_task(fixture.receiver())
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    upstream.bind(str(root / "upstream"))
    writer = None
    try:
        while not any(row["event"] == "receiver-ready" for row in rows(cfg["record"])):
            await asyncio.sleep(0.005)
        Path(cfg["anchor_record"]).write_text(
            json.dumps(dict(event="anchor-ready", **identity())) + "\n"
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
            control.connect(fixture.control)
            fd = os.open(str(root / "upstream"), os.O_PATH | os.O_CLOEXEC)
            try:
                control.sendmsg(
                    [b"F"],
                    [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))],
                )
            finally:
                os.close(fd)
        while not any(
            row["event"] == "binding-complete" for row in rows(cfg["record"])
        ):
            await asyncio.sleep(0.005)
        _, writer = await asyncio.open_unix_connection(fixture.path)

        async def wait():
            while not fixture.file(".sealed.json").exists():
                if process.done():
                    await process
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait(), 5)
        ack = json.loads(fixture.file(".sealed.json").read_text())
        assert ack["taskCount"] == ack["writerCount"] == ack["forwardedBytes"] == 0
        fixture.file(".retire").touch()
        await asyncio.wait_for(process, 5)
        print(
            json.dumps(
                dict(
                    kind="standard-unix-unstarted-connection-task",
                    passed=True,
                    ack=ack,
                    rows=rows(cfg["record"]),
                )
            )
        )
    except BaseException as error:
        print(
            json.dumps(
                dict(
                    kind="standard-unix-unstarted-connection-task",
                    passed=False,
                    error=repr(error),
                    tasks=len(fixture.tasks),
                    writers=len(fixture.writers),
                    rows=rows(cfg["record"]),
                )
            )
        )
        raise
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
        if not process.done():
            process.cancel()
            await asyncio.gather(process, return_exceptions=True)
        upstream.close()
        assert (
            root.resolve() == root
            and root.parent == Path("/home/plumwsl")
            and root.name.startswith(".g0r4check-")
        )
        for directory in (root / "scope",):
            if directory.exists():
                for name in ("control", "interop"):
                    path = directory / name
                    if os.path.lexists(path):
                        path.unlink()
                directory.rmdir()
        for path in root.iterdir():
            assert path.is_file() or path.is_socket()
            path.unlink()
        root.rmdir()


if __name__ == "__main__":
    asyncio.run(main())
