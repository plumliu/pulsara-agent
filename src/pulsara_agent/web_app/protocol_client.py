"""Private Unix-socket client used by the localhost browser bridge.

The launch capability never crosses this module's Python boundary.  Browser
requests are translated into attached Protocol-v3 frames after authentication.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Final, Literal
from uuid import uuid4

from google.protobuf.message import Message

from pulsara_agent.terminal_protocol.generated_v3 import (
    terminal_kernel_v3_pb2 as wire,
)
from pulsara_agent.terminal_protocol.canonical_v3 import MAXIMUM_CONTROL_ITEMS
from pulsara_agent.terminal_protocol.v3_gateway import (
    MAXIMUM_FRAME_BYTES,
    TerminalKernelProtocolServer,
    protocol_identity,
)


class ProtocolBridgeError(RuntimeError):
    """Typed, browser-safe Protocol-v3 failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.public_message = message
        super().__init__(f"{code}: {message}")


class ProtocolTransportClosed(ProtocolBridgeError):
    def __init__(self) -> None:
        super().__init__("PROTOCOL_TRANSPORT_CLOSED", "本地连接已经关闭。")


_ATTACHED_FIELDS: Final = frozenset(
    {
        "snapshot",
        "history_page",
        "observe",
        "command",
        "query_command",
        "read_content",
        "read_tool_artifact",
        "heartbeat",
        "live_control_snapshot",
        "resolve_interaction",
        "resolve_plan_interaction",
        "read_plan_question",
        "read_plan_draft",
    }
)


def _request_id() -> str:
    return f"web-request:{uuid4().hex}"


class TerminalProtocolClient:
    """One sequential Protocol-v3 attachment over a private Unix stream."""

    def __init__(
        self,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        role: int,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.role = role
        self.attachment_id = ""
        self.attachment_generation = 0
        self._lock = asyncio.Lock()
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @classmethod
    async def connect(
        cls,
        *,
        server: TerminalKernelProtocolServer,
        host_session_id: str,
        session_id: str,
        role: int,
        client_instance_id: str,
    ) -> tuple["TerminalProtocolClient", wire.HelloAccepted]:
        try:
            reader, writer = await asyncio.open_unix_connection(
                path=str(server.socket_path)
            )
        except (ConnectionError, OSError) as exc:
            raise ProtocolTransportClosed() from exc
        client = cls(reader=reader, writer=writer, role=role)
        try:
            response = await client._round_trip(
                wire.ClientFrame(
                    hello=wire.HelloRequest(
                        request_id=_request_id(),
                        protocol=protocol_identity(),
                        launch_id=server.launch_id,
                        launch_capability=server.launch_capability,
                        client_instance_id=client_instance_id,
                        host_session_id=host_session_id,
                        session_id=session_id,
                        requested_role=role,
                    )
                )
            )
            kind = response.WhichOneof("response")
            if kind == "error":
                raise ProtocolBridgeError(
                    response.error.stable_code,
                    "本地服务暂时无法完成这次请求。",
                )
            if kind != "hello":
                raise ProtocolBridgeError(
                    "PROTOCOL_RESPONSE_INVALID",
                    "本地服务返回的数据不完整。",
                )
            hello = response.hello
            client.attachment_id = hello.attachment_id
            client.attachment_generation = hello.attachment_generation
            return client, hello
        except BaseException:
            await client.aclose()
            raise

    async def request(self, field: str, message: Message) -> wire.ServerFrame:
        if field not in _ATTACHED_FIELDS:
            raise ValueError(f"unsupported attached Protocol-v3 request: {field}")
        if self._closing or self._closed:
            raise ProtocolTransportClosed()
        if hasattr(message, "request_id") and not getattr(message, "request_id"):
            setattr(message, "request_id", _request_id())
        setattr(message, "attachment_id", self.attachment_id)
        setattr(message, "attachment_generation", self.attachment_generation)
        frame = wire.ClientFrame()
        getattr(frame, field).CopyFrom(message)
        async with self._lock:
            if self._closing or self._closed:
                raise ProtocolTransportClosed()
            return await self._round_trip(frame)

    async def require(
        self, field: str, message: Message, expected_response: str
    ) -> wire.ServerFrame:
        response = await self.request(field, message)
        kind = response.WhichOneof("response")
        if kind == "error":
            raise ProtocolBridgeError(
                response.error.stable_code,
                "本地服务暂时无法完成这次请求。",
            )
        if kind != expected_response:
            raise ProtocolBridgeError(
                "PROTOCOL_RESPONSE_INVALID",
                "本地服务返回的数据不完整。",
            )
        return response

    @property
    def is_open(self) -> bool:
        """Return whether this exact attachment can still serve requests."""

        return (
            not self._closing
            and not self._closed
            and not self._writer.is_closing()
            and not self._reader.at_eof()
        )

    async def _round_trip(self, frame: wire.ClientFrame) -> wire.ServerFrame:
        try:
            payload = frame.SerializeToString(deterministic=True)
            if not 1 <= len(payload) <= MAXIMUM_FRAME_BYTES:
                raise ProtocolBridgeError(
                    "PROTOCOL_FRAME_OUT_OF_BOUNDS",
                    "这次请求包含的数据过多。",
                )
            self._writer.write(len(payload).to_bytes(4, "big") + payload)
            await self._writer.drain()
            header = await self._reader.readexactly(4)
            size = int.from_bytes(header, "big")
            if not 1 <= size <= MAXIMUM_FRAME_BYTES:
                raise ProtocolBridgeError(
                    "PROTOCOL_FRAME_OUT_OF_BOUNDS",
                    "本地服务返回的数据大小异常。",
                )
            response = wire.ServerFrame()
            response.ParseFromString(await self._reader.readexactly(size))
            if response.WhichOneof("response") is None:
                raise ProtocolBridgeError(
                    "PROTOCOL_RESPONSE_INVALID",
                    "本地服务没有返回数据。",
                )
            return response
        except asyncio.CancelledError:
            self._abort_transport()
            raise
        except ProtocolBridgeError:
            self._abort_transport()
            raise
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
            self._abort_transport()
            raise ProtocolTransportClosed() from exc

    def _abort_transport(self) -> None:
        if self._closed:
            return
        self._closing = True
        self._writer.close()

    async def aclose(self) -> None:
        task = self._close_task
        if task is None:
            task = asyncio.create_task(
                self._close_owner(), name=f"terminal-v3-client-close:{id(self)}"
            )
            self._close_task = task
        await asyncio.shield(task)

    async def _close_owner(self) -> None:
        if self._closed:
            return
        self._closing = True
        # When no long poll owns the stream, make the logical detach explicit.
        # A busy stream is closed physically; the gateway's finally block owns
        # the exact same attachment release.
        if not self._lock.locked() and self.attachment_id:
            async with self._lock:
                with suppress(BaseException):
                    command_id = f"command:detach:{uuid4().hex}"
                    await self._round_trip(
                        wire.ClientFrame(
                            command=wire.CommandRequest(
                                request_id=_request_id(),
                                attachment_id=self.attachment_id,
                                attachment_generation=self.attachment_generation,
                                command_id=command_id,
                                client_submission_id=command_id,
                                command_kind=wire.DETACH,
                            )
                        )
                    )
        self._writer.close()
        with suppress(BaseException):
            await self._writer.wait_closed()
        self.attachment_id = ""
        self.attachment_generation = 0
        self._closed = True


class BrowserRuntimeConnection:
    """One browser generation backed by a controller and polling attachment."""

    def __init__(
        self,
        *,
        connection_id: str,
        generation: int,
        role: Literal["controller", "observer"],
        session_id: str,
        host_session_id: str,
        controller: TerminalProtocolClient,
        observer: TerminalProtocolClient,
        hello: wire.HelloAccepted,
        live_hello: wire.HelloAccepted,
        initial_snapshot: wire.ServerFrame,
        initial_live_control: wire.ServerFrame,
    ) -> None:
        self.connection_id = connection_id
        self.generation = generation
        self.role = role
        self.session_id = session_id
        self.host_session_id = host_session_id
        self.controller = controller
        self.observer = observer
        self.hello = hello
        self.live_hello = live_hello
        self.initial_snapshot = initial_snapshot
        self.initial_live_control = initial_live_control
        self._close_task: asyncio.Task[None] | None = None

    @classmethod
    async def open(
        cls,
        *,
        server: TerminalKernelProtocolServer,
        session_id: str,
        host_session_id: str,
        generation: int,
        role: Literal["controller", "observer"] = "controller",
    ) -> "BrowserRuntimeConnection":
        connection_id = f"browser-connection:{uuid4().hex}"
        control_role = (
            wire.ATTACHMENT_ROLE_CONTROLLER
            if role == "controller"
            else wire.ATTACHMENT_ROLE_OBSERVER
        )
        controller, hello = await TerminalProtocolClient.connect(
            server=server,
            host_session_id=host_session_id,
            session_id=session_id,
            role=control_role,
            client_instance_id=f"{connection_id}:controller",
        )
        observer: TerminalProtocolClient | None = None
        try:
            observer, live_hello = await TerminalProtocolClient.connect(
                server=server,
                host_session_id=host_session_id,
                session_id=session_id,
                role=wire.ATTACHMENT_ROLE_OBSERVER,
                client_instance_id=f"{connection_id}:observer",
            )
            snapshot = await controller.require(
                "snapshot",
                wire.SnapshotRequest(
                    maximum_entries=256,
                    maximum_control_items=MAXIMUM_CONTROL_ITEMS,
                ),
                "snapshot",
            )
            # Live-control subscriptions are attachment-local.  The same
            # observer that owns long-poll observation must establish this
            # baseline, otherwise every observation correctly reports a gap.
            live_control = await observer.require(
                "live_control_snapshot",
                wire.LiveControlSnapshotRequest(),
                "live_control_snapshot",
            )
            return cls(
                connection_id=connection_id,
                generation=generation,
                role=role,
                session_id=session_id,
                host_session_id=host_session_id,
                controller=controller,
                observer=observer,
                hello=hello,
                live_hello=live_hello,
                initial_snapshot=snapshot,
                initial_live_control=live_control,
            )
        except BaseException:
            if observer is not None:
                await observer.aclose()
            await controller.aclose()
            raise

    async def aclose(self) -> None:
        task = self._close_task
        if task is None:
            task = asyncio.create_task(
                self._close_owner(),
                name=f"browser-runtime-close:{self.connection_id}",
            )
            self._close_task = task
        await asyncio.shield(task)

    @property
    def is_open(self) -> bool:
        """A browser generation is usable only while both attachments are live."""

        return self.controller.is_open and self.observer.is_open

    async def _close_owner(self) -> None:
        await asyncio.gather(
            self.observer.aclose(), self.controller.aclose(), return_exceptions=True
        )


__all__ = [
    "BrowserRuntimeConnection",
    "ProtocolBridgeError",
    "ProtocolTransportClosed",
    "TerminalProtocolClient",
]
