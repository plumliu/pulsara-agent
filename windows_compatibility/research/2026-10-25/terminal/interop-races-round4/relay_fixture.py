"""G0 research only: anchor-owned fd transfer and opaque asyncio byte relay.

No WSL message parsing. CLI: receiver CONFIG or anchor CONFIG. The real
TerminalManager/Windows Job fixture owns admission, cancellation and final join.
"""

import array
import asyncio
import json
import os
from pathlib import Path
import select
import socket
import stat
import struct
import sys
import time


def identity(pid=None):
    pid = os.getpid() if pid is None else pid
    text = Path(f"/proc/{pid}/stat").read_text()
    tail = text.rsplit(")", 1)[1].split()
    return dict(pid=pid, starttime=tail[19], pgid=int(tail[2]), sid=int(tail[3]))


def rows(path):
    path = Path(path)
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


class Fixture:
    def __init__(self, config, role):
        self.config = config
        self.prefix = str(config["prefix"])
        self.record = Path(
            config["anchor_record"] if role == "anchor" else config["record"]
        )
        self.gates = set(config.get("gates", []))
        self.directory = Path(config["directory"])
        self.control = str(self.directory / "control")
        self.path = str(self.directory / "interop")
        self.sealed = False
        self.cancelled = False
        self.tasks = set()
        self.writers = set()
        self.server = None
        self.listener = None
        self.control_connection = None
        self.binding = None
        self.fd = None
        self.pidfd = None
        self.accepted = 0
        self.forwarded = 0
        self.operation_accepted = 0
        self.operation_forwarded = 0

    def log(self, event, **facts):
        with self.record.open("a", encoding="utf-8") as output:
            output.write(
                json.dumps(dict(event=event, time=time.time(), **identity(), **facts))
                + "\n"
            )

    def file(self, suffix):
        return Path(self.prefix + suffix)

    def cancelled_on_disk(self):
        return self.file(".cancel").exists()

    async def gate(self, name, operation=True):
        if operation and name in self.gates:
            self.log("gate-reached", gate=name)
            while not self.file(".gate." + name + ".release").exists():
                if self.cancelled or self.cancelled_on_disk():
                    raise asyncio.CancelledError
                await asyncio.sleep(0.005)

    def anchor_gate(self, name):
        if name in self.gates:
            self.log("gate-reached", gate=name)
            while not self.file(".gate." + name + ".release").exists():
                if self.cancelled_on_disk():
                    self.log("anchor-cancelled-at-gate", gate=name)
                    return False
                time.sleep(0.005)
        return not self.cancelled_on_disk()

    def anchor(self):
        self.log("anchor-ready", socket=os.environ["WSL_INTEROP"], uid=os.getuid())
        if not self.anchor_gate("anchor-before-pin"):
            return
        fd = os.open(os.environ["WSL_INTEROP"], os.O_PATH | os.O_CLOEXEC)
        try:
            self.log("anchor-pinned", inode=os.fstat(fd).st_ino)
            if not self.anchor_gate("anchor-after-pin"):
                return
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
                control.connect(self.control)
                if self.cancelled_on_disk():
                    self.log("anchor-cancelled-before-send")
                    return
                count = control.sendmsg(
                    [b"F"],
                    [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))],
                )
                assert count == 1
                self.log("anchor-fd-sent")
            # Natural EOF is supplied by the Windows scope owner, not a timer.
            sys.stdin.buffer.read()
            self.log("anchor-eof")
        finally:
            os.close(fd)

    async def recv_fd(self, connection):
        loop = asyncio.get_running_loop()
        while True:
            try:
                return connection.recvmsg(
                    1,
                    socket.CMSG_SPACE(array.array("i").itemsize),
                    socket.MSG_CMSG_CLOEXEC,
                )
            except BlockingIOError:
                readable = loop.create_future()
                fileno = connection.fileno()

                def ready():
                    if not readable.done():
                        readable.set_result(None)

                loop.add_reader(fileno, ready)
                try:
                    await readable
                finally:
                    loop.remove_reader(fileno)

    async def bind(self):
        loop = asyncio.get_running_loop()
        connection, _ = await loop.sock_accept(self.listener)
        self.control_connection = connection
        connection.setblocking(False)
        peer_pid, peer_uid, _ = struct.unpack(
            "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        # Open pidfd before reading /proc, so the received peer remains an exact object.
        self.pidfd = os.pidfd_open(peer_pid)
        peer = identity(peer_pid)
        self.log("control-peer-accepted", peer=peer, peerUid=peer_uid)
        data, ancillary, flags, _ = await self.recv_fd(connection)
        assert data == b"F" and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
        assert len(ancillary) == 1
        level, kind, packed = ancillary[0]
        assert (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
        descriptors = array.array("i")
        descriptors.frombytes(packed)
        assert len(descriptors) == 1
        self.fd = descriptors[0]
        expected = [
            row
            for row in rows(self.config["anchor_record"])
            if row["event"] == "anchor-ready"
        ][-1]
        assert peer_uid == os.getuid() and peer_pid == expected["pid"]
        assert peer["starttime"] == expected["starttime"]
        assert stat.S_ISSOCK(os.fstat(self.fd).st_mode)
        self.log(
            "fd-received",
            peer=peer,
            peerUid=peer_uid,
            inode=os.fstat(self.fd).st_ino,
            recvFlags=flags,
        )
        await self.gate("before-bind")
        if self.cancelled or select.select([self.pidfd], [], [], 0)[0]:
            self.log("binding-rejected", anchorAlive=False)
            return
        # Retain ownership before the first serving await, including cancellation
        # during start_serving. CPython 3.12's non-serving AF_UNIX bind has no await.
        self.server = await asyncio.start_unix_server(
            self.connected, path=self.path, start_serving=False
        )
        # Cancellation can arrive during start_unix_server's await. This task is
        # tracked and gathered before ACK; a server created meanwhile is closed.
        if self.cancelled:
            self.server.close()
            await self.server.wait_closed()
            return
        await self.server.start_serving()
        self.log(
            "relay-ready",
            socket=self.path,
            directory=str(self.directory),
            peer=peer,
            pinned=f"/proc/{os.getpid()}/fd/{self.fd}",
        )
        await self.gate("after-bind")
        self.log("binding-complete")

    def connected(self, reader, writer):
        # Synchronous callback registers a connection before its first await.
        if self.sealed:
            writer.transport.abort()
            self.log("connection-refused-after-seal")
            return
        operation = self.file(".operation-admit").exists()
        self.accepted += 1
        self.operation_accepted += int(operation)
        self.writers.add(writer)
        task = asyncio.create_task(self.connection(reader, writer, operation))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def connection(self, reader, writer, operation):
        remote = None
        try:
            await self.gate("before-upstream", operation)
            other, remote = await asyncio.open_unix_connection(
                f"/proc/{os.getpid()}/fd/{self.fd}"
            )
            self.writers.add(remote)
            await self.gate("after-upstream", operation)
            if self.cancelled:
                return

            async def pump(source, destination, upstream):
                first = True
                total = 0
                while True:
                    half = (
                        upstream
                        and first
                        and operation
                        and "half-forwarded" in self.gates
                    )
                    chunk = await source.read(1 if half else 65536)
                    if not chunk:
                        break
                    if upstream and first:
                        await self.gate("before-forward", operation)
                    # No await from this cancellation check to write. Seal cancels
                    # the whole task and aborts transport queues before its ACK.
                    if self.cancelled:
                        return
                    destination.write(chunk)
                    if upstream:
                        self.forwarded += len(chunk)
                        self.operation_forwarded += len(chunk) if operation else 0
                    await destination.drain()
                    total += len(chunk)
                    if upstream:
                        self.log(
                            "bytes-forwarded",
                            operation=operation,
                            bytes=len(chunk),
                            total=total,
                        )
                        if first and half:
                            await self.gate("half-forwarded", operation)
                        if first:
                            await self.gate("after-forwarded", operation)
                    first = False
                if destination.can_write_eof():
                    destination.write_eof()

            # TaskGroup owns both pumps, so cancel does not orphan a sibling.
            async with asyncio.TaskGroup() as pumps:
                pumps.create_task(pump(reader, remote, True))
                pumps.create_task(pump(other, writer, False))
            self.log("connection-settled", operation=operation)
        except asyncio.CancelledError:
            self.log("connection-cancelled", operation=operation)
            raise
        except Exception as error:
            self.log("connection-error", operation=operation, error=repr(error))
        finally:
            closing = []
            for stream in (writer, remote):
                if stream:
                    stream.transport.abort()
                    closing.append(stream)
            if closing:
                await asyncio.gather(
                    *(stream.wait_closed() for stream in closing),
                    return_exceptions=True,
                )
                self.writers.difference_update(closing)

    async def seal(self, cancel):
        self.sealed = True
        self.cancelled = self.cancelled or cancel
        if self.server:
            self.server.close()
        if self.binding and not self.binding.done():
            self.binding.cancel()
        if cancel:
            for stream in tuple(self.writers):
                stream.transport.abort()
            for task in tuple(self.tasks):
                task.cancel()
        if self.binding:
            await asyncio.gather(self.binding, return_exceptions=True)
        # Let sock_accept/recvmsg cancellation remove their registered readers
        # before closing (and potentially reusing) those file descriptor numbers.
        if self.listener:
            self.listener.close()
        if self.control_connection:
            self.control_connection.close()
        # A bind await may have installed the listener after the earlier close.
        if self.server:
            self.server.close()
        owned_tasks = tuple(self.tasks)
        if owned_tasks:
            await asyncio.gather(*owned_tasks, return_exceptions=True)
            assert all(task.done() for task in owned_tasks)
            self.tasks.difference_update(owned_tasks)
        # A registered task can be cancelled before its coroutine's first step;
        # then its finally never runs. The scope still owns those exact writers.
        closing = tuple(self.writers)
        for stream in closing:
            stream.transport.abort()
        if closing:
            await asyncio.gather(
                *(stream.wait_closed() for stream in closing), return_exceptions=True
            )
            self.writers.difference_update(closing)
        if self.server:
            await self.server.wait_closed()
        if self.tasks or self.writers:
            self.log(
                "seal-unsettled-assertion",
                tasks=[
                    dict(
                        done=task.done(),
                        cancelled=task.cancelled(),
                        coroutine=str(task.get_coro()),
                    )
                    for task in self.tasks
                ],
                writers=[
                    dict(closing=stream.is_closing(), transport=repr(stream.transport))
                    for stream in self.writers
                ],
            )
        assert not self.tasks and not self.writers
        snapshot = dict(
            mode="cancel" if self.cancelled else "normal",
            accepted=self.accepted,
            taskCount=len(self.tasks),
            writerCount=len(self.writers),
            forwardedBytes=self.forwarded,
            anyForwarded=bool(self.forwarded),
            operationAccepted=self.operation_accepted,
            operationForwardedBytes=self.operation_forwarded,
            operationAnyForwarded=bool(self.operation_forwarded),
            socketExists=os.path.lexists(self.path),
            directory=str(self.directory),
        )
        self.log("relay-sealed", **snapshot)
        temporary = self.file(".sealed.tmp")
        temporary.write_text(json.dumps(snapshot))
        temporary.replace(self.file(".sealed.json"))

    async def receiver(self):
        assert (
            self.directory.is_absolute() and self.directory.resolve() == self.directory
        )
        assert self.directory.parent.stat().st_uid == os.getuid()
        os.mkdir(self.directory, 0o700)
        assert stat.S_IMODE(self.directory.stat().st_mode) == 0o700
        assert len(self.control.encode()) < 108 and len(self.path.encode()) < 108
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.setblocking(False)
        self.listener.bind(self.control)
        self.listener.listen()
        self.log(
            "receiver-ready",
            control=self.control,
            directory=str(self.directory),
            mode="0o700",
        )
        self.binding = asyncio.create_task(self.bind())
        sealing = None
        try:
            while True:
                cancel = self.cancelled_on_disk()
                normal = self.file(".seal").exists()
                if (cancel and not self.cancelled) or (
                    (normal or cancel) and sealing is None
                ):
                    if sealing and not sealing.done():
                        # Upgrade normal settlement to cancellation without losing
                        # its already-tracked children to a cancelled gather.
                        self.cancelled = True
                        for stream in tuple(self.writers):
                            stream.transport.abort()
                        for task in tuple(self.tasks):
                            task.cancel()
                    else:
                        sealing = asyncio.create_task(self.seal(cancel))
                if self.binding.done() and not self.binding.cancelled():
                    error = self.binding.exception()
                    if error:
                        self.log("binding-error", error=repr(error))
                        raise error
                if sealing and sealing.done():
                    await sealing
                    if self.file(".retire").exists():
                        self.log(
                            "retire-authorized-by-owner",
                            socketExists=os.path.lexists(self.path),
                        )
                        for path in (self.path, self.control):
                            if os.path.lexists(path):
                                os.unlink(path)
                        self.directory.rmdir()
                        self.log(
                            "relay-retired", directoryExists=self.directory.exists()
                        )
                        return
                await asyncio.sleep(0.005)
        finally:
            if self.listener:
                self.listener.close()
            if self.control_connection:
                self.control_connection.close()
            for fd in (self.fd, self.pidfd):
                if fd is not None:
                    os.close(fd)


def main():
    role, config_path = sys.argv[1:]
    fixture = Fixture(json.loads(Path(config_path).read_text()), role)
    if role == "anchor":
        fixture.anchor()
    else:
        assert role == "receiver"
        asyncio.run(fixture.receiver())


if __name__ == "__main__":
    main()
