"""G0-only standard AF_UNIX SCM_RIGHTS admission, followed by opaque bytes."""

import array
import asyncio
import json
import os
from pathlib import Path
import select
import socket
import struct
import sys
import tempfile
import time

role, record, root, stage, gate, expected = sys.argv[1:]
output = open(record, "a", buffering=1)


def log(event, **facts):
    output.write(
        json.dumps(
            dict(
                event=event,
                pid=os.getpid(),
                starttime=Path("/proc/self/stat").read_text().split()[21],
                **facts,
            )
        )
        + "\n"
    )


def pause(event):
    log(event)
    while not Path(gate).exists():
        time.sleep(0.01)


if role == "anchor":
    log("anchor-ready", socket=os.environ["WSL_INTEROP"])
    if stage == "before-pin":
        pause("before-pin-gate")
    fd = os.open(os.environ["WSL_INTEROP"], os.O_PATH | os.O_CLOEXEC)
    log("anchor-pinned", inode=os.fstat(fd).st_ino)
    if stage == "after-pin":
        pause("after-pin-gate")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.connect(root)
    assert (
        connection.sendmsg(
            [b"F"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))]
        )
        == 1
    )
    log("anchor-fd-sent")
    time.sleep(90)
else:
    directory = tempfile.mkdtemp(prefix="fd-relay-", dir=root)
    control = directory + "/control"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(control)
    listener.listen()
    log(
        "receiver-ready",
        directory=directory,
        control=control,
        mode=oct(os.stat(directory).st_mode & 0o777),
    )
    connection, _ = listener.accept()
    peer_pid, peer_uid, peer_gid = struct.unpack(
        "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    )
    data, ancillary, flags, _ = connection.recvmsg(
        1, socket.CMSG_SPACE(array.array("i").itemsize), socket.MSG_CMSG_CLOEXEC
    )
    log(
        "recvmsg-result", data=data.decode(), flags=flags, ancillaryCount=len(ancillary)
    )
    assert (
        data == b"F"
        and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
        and len(ancillary) == 1
    )
    level, kind, packed = ancillary[0]
    assert (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
    descriptors = array.array("i")
    descriptors.frombytes(packed)
    assert len(descriptors) == 1
    fd = descriptors[0]
    identity = [
        json.loads(line)
        for line in Path(expected).read_text().splitlines()
        if json.loads(line)["event"] == "anchor-ready"
    ][-1]
    pidfd = os.pidfd_open(peer_pid)
    actual = Path(f"/proc/{peer_pid}/stat").read_text().split()[21]
    assert (
        peer_uid == os.getuid()
        and peer_pid == identity["pid"]
        and actual == identity["starttime"]
    )
    log(
        "fd-received",
        peerPid=peer_pid,
        peerUid=peer_uid,
        peerStarttime=actual,
        inode=os.fstat(fd).st_ino,
        pidfdAlive=not bool(select.select([pidfd], [], [], 0)[0]),
    )
    pause("receiver-bind-gate")
    if select.select([pidfd], [], [], 0)[0]:
        log("binding-rejected-anchor-dead", operationAdmitted=False)
        sys.exit(0)

    async def main():
        path = directory + "/interop"
        pinned = f"/proc/{os.getpid()}/fd/{fd}"

        async def connected(reader, writer):
            remote = None
            try:
                other, remote = await asyncio.open_unix_connection(pinned)

                async def pump(source, destination):
                    while chunk := await source.read(65536):
                        destination.write(chunk)
                        await destination.drain()
                    if destination.can_write_eof():
                        destination.write_eof()

                await asyncio.gather(pump(reader, remote), pump(other, writer))
                log("connection-complete")
            except Exception as error:
                log("connection-error", kind=type(error).__name__, message=str(error))
            finally:
                for stream in (writer, remote):
                    if stream:
                        stream.close()

        server = await asyncio.start_unix_server(connected, path=path)
        log(
            "receiver-bound",
            socket=path,
            directory=directory,
            peerPid=peer_pid,
            pinned=pinned,
            operationAdmitted=True,
        )
        async with server:
            await server.serve_forever()

    asyncio.run(main())
