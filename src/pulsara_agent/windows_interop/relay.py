"""Opaque standard Unix streams; WSL retains its protocol and PE launcher."""

from __future__ import annotations

import array
import asyncio
import os
from pathlib import Path
import select
import socket
import stat
import struct


class InteropRelay:
    def __init__(self, directory: Path) -> None:
        self.control_path = directory / "c"
        self.path = directory / "s"
        self.listener: socket.socket | None = None
        self.control_connection: socket.socket | None = None
        self.server: asyncio.Server | None = None
        self.binding: asyncio.Task[None] | None = None
        self.descriptor: int | None = None
        self.peer_pidfd: int | None = None
        self.peer_identity: tuple[int, str] | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.sealed = False
        self.cancelled = False

    def start(self) -> asyncio.Task[None]:
        if max(len(os.fsencode(self.path)), len(os.fsencode(self.control_path))) >= 108:
            raise ValueError(
                "interop control path exceeds the Linux Unix socket boundary"
            )
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener = listener
        listener.setblocking(False)
        listener.bind(str(self.control_path))
        listener.listen()
        self.binding = asyncio.create_task(self._bind(), name="interop-fd-binding")
        return self.binding

    async def _receive_descriptor(self, connection: socket.socket):
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
                descriptor = connection.fileno()

                def ready() -> None:
                    if not readable.done():
                        readable.set_result(None)

                loop.add_reader(descriptor, ready)
                try:
                    await readable
                finally:
                    loop.remove_reader(descriptor)

    async def _bind(self) -> None:
        assert self.listener is not None
        connection, _ = await asyncio.get_running_loop().sock_accept(self.listener)
        self.control_connection = connection
        connection.setblocking(False)
        peer_pid, peer_uid, _ = struct.unpack(
            "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        if peer_uid != os.getuid():
            raise RuntimeError("interop anchor has a different Linux user")
        self.peer_pidfd = os.pidfd_open(peer_pid)
        start = Path(f"/proc/{peer_pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        self.peer_identity = (peer_pid, start)
        data, ancillary, flags, _ = await self._receive_descriptor(connection)
        received = array.array("i")
        for level, kind, packed in ancillary:
            if (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS):
                received.frombytes(packed)
        if (
            data != b"F"
            or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
            or len(ancillary) != 1
            or len(received) != 1
        ):
            for descriptor in received:
                os.close(descriptor)
            raise RuntimeError("invalid interop anchor descriptor transfer")
        self.descriptor = received[0]
        if not stat.S_ISSOCK(os.fstat(self.descriptor).st_mode):
            raise RuntimeError("interop anchor did not transfer a Unix socket")
        if self.cancelled or select.select([self.peer_pidfd], [], [], 0)[0]:
            raise RuntimeError("interop anchor exited before binding")
        self.server = await asyncio.start_unix_server(
            self._connected, path=str(self.path), start_serving=False
        )
        if self.cancelled:
            raise asyncio.CancelledError
        await self.server.start_serving()

    def _connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # The callback owns its writer before the coroutine's first step. A cold
        # cancelled task need not run finally, so seal also closes owned writers.
        self.writers.add(writer)
        coroutine = self._connection(reader, writer, admitted=not self.sealed)
        try:
            task = asyncio.create_task(coroutine, name="interop-byte-relay")
        except BaseException:
            coroutine.close()
            writer.transport.abort()
            raise
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _connection(self, reader, writer, *, admitted: bool) -> None:
        remote = None
        try:
            if not admitted:
                return
            assert self.descriptor is not None
            other, remote = await asyncio.open_unix_connection(
                f"/proc/self/fd/{self.descriptor}"
            )
            self.writers.add(remote)
            if self.cancelled:
                return

            async def pump(source, destination):
                while chunk := await source.read(64 * 1024):
                    if self.cancelled:
                        return
                    destination.write(chunk)
                    await destination.drain()
                if destination.can_write_eof():
                    destination.write_eof()

            async with asyncio.TaskGroup() as pumps:
                pumps.create_task(pump(reader, remote))
                pumps.create_task(pump(other, writer))
        except (OSError, ConnectionError, ExceptionGroup):
            # A closed interop transport is delivered to its original caller;
            # neither reconnect nor any request replay belongs to this relay.
            pass
        finally:
            closing = tuple(stream for stream in (writer, remote) if stream is not None)
            for stream in closing:
                stream.transport.abort()
            await asyncio.gather(
                *(stream.wait_closed() for stream in closing), return_exceptions=True
            )
            self.writers.difference_update(closing)

    def request_cancel(self) -> None:
        self.sealed = True
        self.cancelled = True
        if self.server is not None:
            self.server.close()
        if self.binding is not None and not self.binding.done():
            self.binding.cancel()
        for writer in tuple(self.writers):
            writer.transport.abort()
        for task in tuple(self.tasks):
            task.cancel()

    async def seal(self, *, cancel: bool) -> None:
        self.sealed = True
        if cancel:
            self.request_cancel()
        elif self.server is not None:
            self.server.close()
        if self.binding is not None:
            if not self.binding.done():
                self.binding.cancel()
            await asyncio.gather(self.binding, return_exceptions=True)
        # Remove asyncio readers before closing their descriptor numbers.
        if self.listener is not None:
            self.listener.close()
        if self.control_connection is not None:
            self.control_connection.close()
        if self.server is not None:
            self.server.close()
        while True:
            owned = tuple(self.tasks)
            if owned:
                await asyncio.gather(*owned, return_exceptions=True)
                assert all(task.done() for task in owned)
                self.tasks.difference_update(owned)
            closing = tuple(self.writers)
            for writer in closing:
                writer.transport.abort()
            await asyncio.gather(
                *(writer.wait_closed() for writer in closing), return_exceptions=True
            )
            self.writers.difference_update(closing)
            if self.server is not None:
                await self.server.wait_closed()
            if not self.tasks and not self.writers:
                break
        assert not self.tasks and not self.writers

    def close_descriptors(self) -> None:
        for name in ("descriptor", "peer_pidfd"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)
        # Socket names deliberately survive close. Only the original physical
        # owner can retire them after both operating-system domains have joined.
