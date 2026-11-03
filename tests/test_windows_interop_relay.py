"""Standard Unix API assertions at the opaque relay ownership boundary."""

import asyncio
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory

import pytest

from pulsara_agent.windows_interop.relay import InteropRelay


@pytest.fixture
def relay_directory():
    # AF_UNIX pathname limits apply to the complete pytest temporary path too.
    with TemporaryDirectory(prefix="pr", dir="/tmp") as directory:
        yield Path(directory)


async def bind_relay(directory: Path, handler, monkeypatch):
    """Exercise byte ownership after binding; Linux anchor auth is integration."""
    upstream_path = directory / "up"
    upstream = await asyncio.start_unix_server(handler, path=str(upstream_path))
    relay = InteropRelay(directory)
    relay.descriptor = os.dup(upstream.sockets[0].fileno())
    original_open = asyncio.open_unix_connection

    async def open_owned_descriptor(path, *args, **kwargs):
        # Adapt only the Linux descriptor-path OS boundary. Both sides still
        # use real Unix sockets, writers, backpressure, and relay coroutines.
        if path == f"/proc/self/fd/{relay.descriptor}":
            path = str(upstream_path)
        return await original_open(path, *args, **kwargs)

    monkeypatch.setattr(asyncio, "open_unix_connection", open_owned_descriptor)
    relay.server = await asyncio.start_unix_server(relay._connected, path=str(relay.path))
    return relay, upstream


def test_opaque_bytes_normal_seal_waits_existing_connection_and_preserves_path(
    relay_directory, monkeypatch,
):
    async def scenario():
        arrived = asyncio.Event()

        async def echo(reader, writer):
            try:
                while block := await reader.read(4096):
                    writer.write(block)
                    await writer.drain()
                    arrived.set()
            finally:
                writer.close()
                await writer.wait_closed()

        relay, upstream = await bind_relay(relay_directory, echo, monkeypatch)
        reader, writer = await asyncio.open_unix_connection(str(relay.path))
        payload = bytes(range(256)) * 4
        writer.write(payload)
        await writer.drain()
        assert await reader.readexactly(len(payload)) == payload
        await arrived.wait()
        sealed = asyncio.create_task(relay.seal(cancel=False))
        await asyncio.sleep(0)
        assert not sealed.done()
        writer.write(b"accepted-before-seal")
        await writer.drain()
        assert await reader.readexactly(20) == b"accepted-before-seal"
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(sealed, 2)
        assert not relay.tasks and not relay.writers
        assert relay.path.exists()
        with pytest.raises(ConnectionRefusedError):
            await asyncio.open_unix_connection(str(relay.path))
        relay.close_descriptors()
        assert relay.path.exists()
        upstream.close()
        await upstream.wait_closed()

    asyncio.run(scenario())


def test_cancel_during_upstream_open_cannot_forward_after_settlement(
    relay_directory, monkeypatch
):
    async def scenario():
        received = bytearray()
        gate = asyncio.Event()
        entered = asyncio.Event()
        original_open = asyncio.open_unix_connection

        async def capture(reader, writer):
            try:
                received.extend(await reader.read())
            finally:
                writer.close()
                await writer.wait_closed()

        relay, upstream = await bind_relay(relay_directory, capture, monkeypatch)

        async def gated(path, *args, **kwargs):
            if str(path).startswith("/proc/self/fd/"):
                entered.set()
                await gate.wait()
            return await original_open(path, *args, **kwargs)

        monkeypatch.setattr(asyncio, "open_unix_connection", gated)
        _, writer = await original_open(str(relay.path))
        writer.write(b"must-not-forward")
        await writer.drain()
        await entered.wait()
        await asyncio.wait_for(relay.seal(cancel=True), 2)
        assert not relay.tasks and not relay.writers
        gate.set()
        await asyncio.sleep(0)
        assert not received and relay.path.exists()
        writer.close()
        await writer.wait_closed()
        relay.close_descriptors()
        upstream.close()
        await upstream.wait_closed()

    asyncio.run(scenario())


def test_cancel_collects_registered_writer_before_connection_coroutine_starts(
    relay_directory, monkeypatch,
):
    async def scenario():
        async def unused(reader, writer):
            writer.close()
            await writer.wait_closed()

        relay, upstream = await bind_relay(relay_directory, unused, monkeypatch)
        left, right = socket.socketpair()
        reader, writer = await asyncio.open_connection(sock=left)
        try:
            relay._connected(reader, writer)
            # No checkpoint: the connection task has never entered try/finally.
            relay.request_cancel()
            await asyncio.wait_for(relay.seal(cancel=True), 2)
            assert not relay.tasks and not relay.writers
            assert writer.is_closing() and relay.path.exists()
        finally:
            right.close()
            relay.close_descriptors()
            upstream.close()
            await upstream.wait_closed()

    asyncio.run(scenario())
