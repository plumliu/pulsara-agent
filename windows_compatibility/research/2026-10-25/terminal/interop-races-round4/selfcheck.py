"""Small real AF_UNIX checks for the shared module, not WSL qualification."""

import array
import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
import time  # noqa: F401 -- preserve executed research snapshot
from relay_fixture import Fixture, identity, rows


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), 5)  # probe diagnosis, not a product lifetime cap


async def case(root, gate):
    directory = root / ("s-" + gate)
    normal = gate.startswith("normal")
    config = dict(
        directory=str(directory),
        record=str(root / (gate + ".relay.jsonl")),
        anchor_record=str(root / (gate + ".anchor.jsonl")),
        prefix=str(root / gate),
        gates=[] if normal else [gate],
    )
    fixture = Fixture(config, "receiver")
    receiver = asyncio.create_task(fixture.receiver())
    seen = []
    mock_tasks = set()

    async def upstream(reader, writer):
        mock_tasks.add(asyncio.current_task())
        try:
            seen.append(await reader.read())
        finally:
            writer.close()
            await writer.wait_closed()
            mock_tasks.discard(asyncio.current_task())

    upstream_path = str(root / "upstream")
    server = await asyncio.start_unix_server(upstream, path=upstream_path)
    try:
        await until(
            lambda: any(
                row["event"] == "receiver-ready" for row in rows(config["record"])
            )
        )
        Path(config["anchor_record"]).write_text(
            json.dumps(dict(event="anchor-ready", **identity())) + "\n"
        )
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(str(directory / "control"))
        fd = os.open(upstream_path, os.O_PATH | os.O_CLOEXEC)
        try:
            connection.sendmsg(
                [b"F"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))]
            )
        finally:
            os.close(fd)
            connection.close()
        await until(
            lambda: any(row["event"] == "relay-ready" for row in rows(config["record"]))
        )
        fixture.file(".operation-admit").touch()
        reader, writer = await asyncio.open_unix_connection(fixture.path)
        payload = b"opaque-unix-selfcheck"
        writer.write(payload)
        await writer.drain()
        if normal:
            await until(lambda: fixture.operation_forwarded == len(payload))
            fixture.file(".seal").touch()
            await until(lambda: fixture.sealed)
            assert not fixture.file(".sealed.json").exists(), (
                "normal must await accepted stream EOF"
            )
            if gate == "normal-cancel":
                fixture.file(".cancel").touch()
            else:
                writer.write_eof()
                await reader.read()
        else:
            await until(
                lambda: any(
                    row["event"] == "gate-reached" and row["gate"] == gate
                    for row in rows(config["record"])
                )
            )
            fixture.file(".cancel").touch()
        await until(lambda: fixture.file(".sealed.json").exists())
        ack = json.loads(fixture.file(".sealed.json").read_text())
        assert ack["mode"] == ("normal" if gate == "normal" else "cancel")
        assert ack["taskCount"] == ack["writerCount"] == 0
        assert ack["socketExists"] and os.path.lexists(fixture.path)
        before = fixture.operation_forwarded
        fixture.file(".gate." + gate + ".release").touch()
        try:
            late_r, late_w = await asyncio.open_unix_connection(fixture.path)
        except ConnectionRefusedError:
            refused = True
        else:
            refused = False
            late_w.close()
            await late_w.wait_closed()
        assert refused
        await asyncio.sleep(0.03)
        assert fixture.operation_forwarded == before == ack["operationForwardedBytes"]
        assert not fixture.tasks and not fixture.writers
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass
        if gate in ("before-upstream", "after-upstream", "before-forward"):
            assert before == 0
        if gate == "half-forwarded":
            assert before == 1
        if gate in ("after-forwarded", "normal", "normal-cancel"):
            assert before == len(payload)
        fixture.file(".retire").touch()
        await asyncio.wait_for(receiver, 5)
        assert not directory.exists()
        return dict(
            gate=gate,
            ack=ack,
            bytesStableAfterAck=True,
            lateConnectionRefused=True,
            directoryRemoved=True,
            rows=rows(config["record"]),
        )
    finally:
        if not receiver.done():
            fixture.file(".cancel").touch()
            fixture.file(".retire").touch()
            await asyncio.wait_for(receiver, 5)
        server.close()
        for task in tuple(mock_tasks):
            task.cancel()
        if mock_tasks:
            await asyncio.gather(*tuple(mock_tasks), return_exceptions=True)
        await server.wait_closed()
        if os.path.lexists(upstream_path):
            os.unlink(upstream_path)


async def prepare_cancel(root, stage):
    name = "prepare-" + stage
    config = dict(
        directory=str(root / ("s-" + stage)),
        record=str(root / (name + ".relay.jsonl")),
        anchor_record=str(root / (name + ".anchor.jsonl")),
        prefix=str(root / name),
        gates=[stage],
    )
    fixture = Fixture(config, "receiver")
    receiver = asyncio.create_task(fixture.receiver())
    upstream_path = str(root / "upstream")
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    upstream.bind(upstream_path)
    control = None
    try:
        await until(
            lambda: any(
                row["event"] == "receiver-ready" for row in rows(config["record"])
            )
        )
        if stage != "accept":
            Path(config["anchor_record"]).write_text(
                json.dumps(dict(event="anchor-ready", **identity())) + "\n"
            )
            control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            control.connect(fixture.control)
            await until(
                lambda: any(
                    row["event"] == "control-peer-accepted"
                    for row in rows(config["record"])
                )
            )
            if stage != "recvmsg":
                fd = os.open(upstream_path, os.O_PATH | os.O_CLOEXEC)
                try:
                    control.sendmsg(
                        [b"F"],
                        [
                            (
                                socket.SOL_SOCKET,
                                socket.SCM_RIGHTS,
                                array.array("i", [fd]),
                            )
                        ],
                    )
                finally:
                    os.close(fd)
                await until(
                    lambda: any(
                        row["event"] == "gate-reached" and row["gate"] == stage
                        for row in rows(config["record"])
                    )
                )
        fixture.file(".cancel").touch()
        await until(lambda: fixture.file(".sealed.json").exists())
        ack = json.loads(fixture.file(".sealed.json").read_text())
        assert (
            ack["mode"] == "cancel"
            and ack["taskCount"] == ack["writerCount"] == ack["forwardedBytes"] == 0
        )
        assert fixture.binding.done() and not fixture.tasks
        fixture.file(".gate." + stage + ".release").touch()
        await asyncio.sleep(0.02)
        assert not any(
            row["event"] == "binding-complete" for row in rows(config["record"])
        )
        fixture.file(".retire").touch()
        await asyncio.wait_for(receiver, 5)
        return dict(
            gate=name,
            ack=ack,
            bindingTaskSettled=True,
            noBindingCompleteAfterAck=True,
            rows=rows(config["record"]),
        )
    finally:
        if control:
            control.close()
        if not receiver.done():
            fixture.file(".cancel").touch()
            fixture.file(".retire").touch()
            await asyncio.wait_for(receiver, 5)
        upstream.close()
        os.unlink(upstream_path)


async def main():
    root = Path(tempfile.mkdtemp(prefix=".g0r4check-", dir="/home/plumwsl"))
    results = []
    try:
        for gate in (
            "before-upstream",
            "after-upstream",
            "before-forward",
            "half-forwarded",
            "after-forwarded",
            "normal",
            "normal-cancel",
        ):
            results.append(await case(root, gate))
        for stage in ("accept", "recvmsg", "before-bind", "after-bind"):
            results.append(await prepare_cancel(root, stage))
        print(
            json.dumps(
                dict(
                    kind="standard-unix-only-not-wsl-qualification",
                    cases=results,
                    passed=True,
                )
            )
        )
    finally:
        assert (
            root.resolve() == root
            and root.parent == Path("/home/plumwsl")
            and root.name.startswith(".g0r4check-")
        )
        for path in root.iterdir():
            assert path.is_file() or path.is_socket(), path
            path.unlink()
        root.rmdir()


if __name__ == "__main__":
    asyncio.run(main())
