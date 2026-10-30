from __future__ import annotations
from pulsara_agent.identity import derive_id

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from aiohttp import ClientSession, DummyCookieJar

import pulsara_agent.desktop_app.renderer_bridge as renderer_bridge_module
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer
from pulsara_agent.desktop_app.renderer_bridge import RendererBridge
from pulsara_agent.desktop_app.renderer_bridge import (
    BridgeDetachFailed,
    BridgeDetachFull,
    BridgeSettlementFailed,
    BridgeSettlementFull,
)
from pulsara_agent.desktop_app.protocol_client import (
    _ATTACHED_FIELDS,
    ProtocolBridgeError,
    TerminalProtocolClient,
)
from pulsara_agent.terminal_protocol.v3_gateway import MAXIMUM_FRAME_BYTES
from pulsara_agent.desktop_app.session_controller import (
    HostSessionHandle,
    LocalSessionController,
    NoOldHostReadyToResume,
    OldHostCloseFull,
    PreparedRuntimeReopenOperation,
    PreparedRuntimeResumeOperation,
    SessionControlRejected,
)
from pulsara_agent.desktop_app.http_server import LocalHttpServer
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import test_model_runtime

BROWSER_ONE = "00000000-0000-4000-8000-000000000001"
BROWSER_TWO = "00000000-0000-4000-8000-000000000002"
BROWSER_THREE = "00000000-0000-4000-8000-000000000003"


def test_pr03_background_reads_use_attached_protocol_requests() -> None:
    assert "list_background_processes" in _ATTACHED_FIELDS
    assert "read_background_process_log" in _ATTACHED_FIELDS


class _Sessions:
    def host_writer_unavailable(self, host_session_id: str) -> bool:
        return False

    async def resume_session(self, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            session_id=session_id,
            host_session_id=derive_id("host", "test-fixture", f"host:{session_id}"),
            session=SimpleNamespace(workspace=SimpleNamespace(workspace_root=Path.cwd())),
        )


class _RuntimeConnection:
    next_id = 0

    def __init__(self, *, session_id: str, generation: int, role: str) -> None:
        type(self).next_id += 1
        self.connection_id = f"connection:{type(self).next_id}"
        self.session_id = session_id
        self.host_session_id = derive_id("host", "test-fixture", f"host:{session_id}")
        self.generation = generation
        self.role = role
        self.closed = False
        self.requests: list[tuple[str, object]] = []
        self.controller = self
        self.observer = self
        self.attachment_id = derive_id("attachment", "test-fixture", f"attachment:{generation}")

    @classmethod
    async def open(
        cls,
        *,
        server: object,
        session_id: str,
        host_session_id: str,
        generation: int,
        role: str,
    ) -> "_RuntimeConnection":
        assert server is not None
        assert host_session_id == derive_id("host", "test-fixture", f"host:{session_id}")
        return cls(session_id=session_id, generation=generation, role=role)

    async def aclose(self) -> None:
        self.closed = True

    async def request(self, kind: str, request: object) -> wire.ServerFrame:
        self.requests.append((kind, request))
        request_id = str(getattr(request, "request_id", ""))
        if kind == "command":
            return wire.ServerFrame(
                command_outcome=wire.CommandOutcome(
                    request_id=request_id,
                    command_id=str(getattr(request, "command_id")),
                    status=wire.PENDING,
                )
            )
        if kind == "query_command":
            return wire.ServerFrame(
                query_command=wire.QueryCommandResponse(
                    request_id=request_id,
                    found=False,
                    control_query_status=wire.CONTROL_QUERY_RESULT_UNAVAILABLE,
                )
            )
        if kind == "read_content":
            return wire.ServerFrame(
                content=wire.CanonicalContentChunk(
                    request_id=request_id,
                    digest="sha256:exact",
                    content=b"exact-image-bytes",
                    complete_size=len(b"exact-image-bytes"),
                    complete=True,
                )
            )
        if kind == "list_background_processes":
            return wire.ServerFrame(
                background_processes=wire.BackgroundProcessPage(
                    request_id=request_id,
                    session_id=self.session_id,
                    host_session_id=derive_id("host", "test-fixture", f"host:{self.session_id}"),
                )
            )
        if kind == "read_background_process_log":
            return wire.ServerFrame(
                background_process_log=wire.BackgroundProcessLog(
                    request_id=request_id,
                    session_id=self.session_id,
                    host_session_id=derive_id("host", "test-fixture", f"host:{self.session_id}"),
                    output="exact output",
                )
            )
        raise AssertionError(f"unexpected request kind: {kind}")

    @property
    def is_open(self) -> bool:
        return not self.closed


def test_browser_connections_share_one_controller_without_reconnect_ping_pong(
    monkeypatch,
) -> None:
    asyncio.run(_exercise_browser_connection_roles(monkeypatch))


@pytest.mark.parametrize("removed_during_attachment", [False, True])
def test_connect_observes_the_restored_root_after_attachment_setup(
    monkeypatch, tmp_path, removed_during_attachment,
) -> None:
    async def exercise():
        root = tmp_path / "workspace"
        root.mkdir()

        class Sessions(_Sessions):
            async def resume_session(self, session_id):
                handle = await super().resume_session(session_id)
                handle.session.workspace.workspace_root = root
                return handle

        class Connection(_RuntimeConnection):
            @classmethod
            async def open(cls, **kwargs):
                connection = await super().open(**kwargs)
                if removed_during_attachment:
                    root.rmdir()
                return connection

        monkeypatch.setattr(renderer_bridge_module, "BrowserRuntimeConnection", Connection)
        monkeypatch.setattr(RendererBridge, "_connection_payload", staticmethod(
            lambda connection: {"connection_id": connection.connection_id}
        ))
        bridge = RendererBridge(
            sessions=cast(LocalSessionController, Sessions()),
            protocol_server=cast(TerminalKernelProtocolServer, object()),
        )
        try:
            payload = await bridge.connect(
                'session:5dyf7lfzp7z22pphjtostkgsjm', browser_instance_id=BROWSER_ONE,
            )
            assert payload["workspace_availability"] == {
                "path": str(root),
                "outcome": "MISSING" if removed_during_attachment else "AVAILABLE",
                "reason": None,
            }
            assert str(payload["connection_id"]) in bridge._connections
        finally:
            await bridge.aclose()

    asyncio.run(exercise())


async def _exercise_browser_connection_roles(monkeypatch) -> None:
    _RuntimeConnection.next_id = 0
    monkeypatch.setattr(
        renderer_bridge_module,
        "BrowserRuntimeConnection",
        _RuntimeConnection,
    )
    monkeypatch.setattr(
        RendererBridge,
        "_connection_payload",
        staticmethod(
            lambda connection: {
                "connection_id": connection.connection_id,
                "session_id": connection.session_id,
                "connection_generation": connection.generation,
                "role": connection.role,
            }
        ),
    )
    bridge = RendererBridge(
        sessions=cast(LocalSessionController, _Sessions()),
        protocol_server=cast(TerminalKernelProtocolServer, object()),
    )

    first = await bridge.connect('session:5dyf7lfzp7z22pphjtostkgsjm', browser_instance_id=BROWSER_ONE)
    second = await bridge.connect('session:5dyf7lfzp7z22pphjtostkgsjm', browser_instance_id=BROWSER_TWO)
    other_session = await bridge.connect('session:f2uxxwrmmavsqgv655dvvzytnu', browser_instance_id=BROWSER_ONE)

    assert first["role"] == "controller"
    assert second["role"] == "observer"
    assert other_session["role"] == "controller"
    first_connection = bridge._connections[str(first["connection_id"])]
    second_connection = bridge._connections[str(second["connection_id"])]
    other_connection = bridge._connections[str(other_session["connection_id"])]
    assert not first_connection.closed
    assert not second_connection.closed
    assert not other_connection.closed
    assert bridge._controller_by_session['session:5dyf7lfzp7z22pphjtostkgsjm'] == first["connection_id"]
    assert (
        bridge._controller_by_session['session:f2uxxwrmmavsqgv655dvvzytnu'] == other_session["connection_id"]
    )

    refreshed = await bridge.connect('session:5dyf7lfzp7z22pphjtostkgsjm', browser_instance_id=BROWSER_ONE)

    assert refreshed["role"] == "controller"
    assert first_connection.closed
    assert not second_connection.closed
    assert not other_connection.closed
    assert bridge._controller_by_session['session:5dyf7lfzp7z22pphjtostkgsjm'] == refreshed["connection_id"]
    await bridge.disconnect(str(first["connection_id"]))
    assert bridge._controller_by_session['session:5dyf7lfzp7z22pphjtostkgsjm'] == refreshed["connection_id"]

    refreshed_connection = bridge._connections[str(refreshed["connection_id"])]
    third = await bridge.connect(
        'session:5dyf7lfzp7z22pphjtostkgsjm',
        browser_instance_id=BROWSER_THREE,
        takeover=True,
    )

    assert third["role"] == "controller"
    assert refreshed_connection.closed
    assert not second_connection.closed
    assert not other_connection.closed
    assert str(second["connection_id"]) in bridge._connections
    assert bridge._controller_by_session['session:5dyf7lfzp7z22pphjtostkgsjm'] == third["connection_id"]
    assert (
        bridge._controller_by_session['session:f2uxxwrmmavsqgv655dvvzytnu'] == other_session["connection_id"]
    )

    await bridge.aclose()


def test_dead_attachment_expires_the_whole_browser_generation(monkeypatch) -> None:
    asyncio.run(_exercise_dead_attachment_expiry(monkeypatch))


async def _exercise_dead_attachment_expiry(monkeypatch) -> None:
    _RuntimeConnection.next_id = 0
    monkeypatch.setattr(
        renderer_bridge_module,
        "BrowserRuntimeConnection",
        _RuntimeConnection,
    )
    monkeypatch.setattr(
        RendererBridge,
        "_connection_payload",
        staticmethod(
            lambda connection: {
                "connection_id": connection.connection_id,
                "session_id": connection.session_id,
                "connection_generation": connection.generation,
                "role": connection.role,
            }
        ),
    )
    bridge = RendererBridge(
        sessions=cast(LocalSessionController, _Sessions()),
        protocol_server=cast(TerminalKernelProtocolServer, object()),
    )
    payload = await bridge.connect('session:5dyf7lfzp7z22pphjtostkgsjm', browser_instance_id=BROWSER_ONE)
    connection_id = str(payload["connection_id"])
    connection = bridge._connections[connection_id]
    connection.closed = True

    with pytest.raises(renderer_bridge_module.ProtocolTransportClosed):
        await bridge._connection(connection_id)

    assert connection_id not in bridge._connections
    assert 'session:5dyf7lfzp7z22pphjtostkgsjm' not in bridge._controller_by_session
    replacement = await bridge.connect('session:5dyf7lfzp7z22pphjtostkgsjm', browser_instance_id=BROWSER_ONE)
    assert replacement["role"] == "controller"
    await bridge.aclose()


def test_pr03_renderer_bridge_preserves_exact_control_and_background_read_fields(
    monkeypatch,
) -> None:
    asyncio.run(_exercise_pr03_renderer_bridge_fields(monkeypatch))


async def _exercise_pr03_renderer_bridge_fields(monkeypatch) -> None:
    _RuntimeConnection.next_id = 0
    monkeypatch.setattr(renderer_bridge_module, "BrowserRuntimeConnection", _RuntimeConnection)
    monkeypatch.setattr(
        RendererBridge,
        "_connection_payload",
        staticmethod(
            lambda connection: {
                "connection_id": connection.connection_id,
                "session_id": connection.session_id,
                "connection_generation": connection.generation,
                "role": connection.role,
            }
        ),
    )
    bridge = RendererBridge(
        sessions=cast(LocalSessionController, _Sessions()),
        protocol_server=cast(TerminalKernelProtocolServer, object()),
    )
    payload = await bridge.connect('session:5dyf7lfzp7z22pphjtostkgsjm', browser_instance_id=BROWSER_ONE)
    connection_id = str(payload["connection_id"])
    connection = bridge._connections[connection_id]
    command_id = (
        'command:56oxulp7hzsqkiylu4cl56mbja'
    )

    await bridge.command(
        connection_id,
        {
            "command_id": command_id,
            "command_kind": "TERMINATE_BACKGROUND_PROCESS",
            "control_admission_deadline_ms": 9000,
            "expected_session_id": 'session:5dyf7lfzp7z22pphjtostkgsjm',
            "expected_host_session_id": 'host:7r7elstdigup6xfbo3hd3d5jfu',
            "target_process_id": 'process:77t67dg5pb4j4dif5xd3xcidwe',
        },
    )
    kind, request = connection.requests[-1]
    assert kind == "command"
    assert isinstance(request, wire.CommandRequest)
    assert request.client_submission_id == ""
    assert request.control_admission_deadline_ms == 9000
    assert request.expected_session_id == 'session:5dyf7lfzp7z22pphjtostkgsjm'
    assert request.expected_host_session_id == 'host:7r7elstdigup6xfbo3hd3d5jfu'
    assert request.target_process_id == 'process:77t67dg5pb4j4dif5xd3xcidwe'
    assert not request.target_turn_id
    assert "subagent_task_id" not in request.DESCRIPTOR.fields_by_name

    await bridge.query_command(
        connection_id,
        {
            "command_id": command_id,
            "expected_control": {
                "admission_deadline_ms": 9000,
                "operation": "USER_CONTROL_TERMINATE_BACKGROUND_PROCESS",
                "session_id": 'session:5dyf7lfzp7z22pphjtostkgsjm',
                "host_session_id": 'host:7r7elstdigup6xfbo3hd3d5jfu',
                "target_kind": "USER_CONTROL_BACKGROUND_PROCESS",
                "target_id": 'process:77t67dg5pb4j4dif5xd3xcidwe',
            },
        },
    )
    kind, query = connection.requests[-1]
    assert kind == "query_command"
    assert isinstance(query, wire.QueryCommandRequest)
    assert query.expected_control.admission_deadline_ms == 9000
    assert query.expected_control.target.target_id == 'process:77t67dg5pb4j4dif5xd3xcidwe'
    assert query.expected_control.host_session_id == 'host:7r7elstdigup6xfbo3hd3d5jfu'

    await bridge.command(connection_id, {
        "command_id": command_id,
        "command_kind": "STOP_CONTEXT_COMPACTION",
        "control_admission_deadline_ms": 9000,
        "expected_session_id": 'session:5dyf7lfzp7z22pphjtostkgsjm',
        "expected_host_session_id": 'host:7r7elstdigup6xfbo3hd3d5jfu',
        "target_compaction_command_id": 'command:manual-original',
    })
    kind, manual_stop = connection.requests[-1]
    assert kind == "command"
    assert manual_stop.command_kind == wire.STOP_CONTEXT_COMPACTION
    assert manual_stop.target_compaction_command_id == 'command:manual-original'
    assert not manual_stop.target_turn_id
    assert not manual_stop.client_submission_id
    await bridge.query_command(connection_id, {
        "command_id": command_id,
        "expected_control": {
            "admission_deadline_ms": 9000,
            "operation": "USER_CONTROL_STOP_CONTEXT_COMPACTION",
            "session_id": 'session:5dyf7lfzp7z22pphjtostkgsjm',
            "host_session_id": 'host:7r7elstdigup6xfbo3hd3d5jfu',
            "target_kind": "USER_CONTROL_MANUAL_COMPACTION",
            "target_id": 'command:manual-original',
        },
    })
    kind, manual_query = connection.requests[-1]
    assert kind == "query_command"
    assert manual_query.expected_control.operation == wire.USER_CONTROL_STOP_CONTEXT_COMPACTION
    assert manual_query.expected_control.target.kind == wire.USER_CONTROL_MANUAL_COMPACTION
    assert manual_query.expected_control.target.target_id == 'command:manual-original'
    with pytest.raises(ValueError, match="unexpected field"):
        await bridge.command(connection_id, {
            "command_id": command_id, "command_kind": "COMPACT_CONTEXT", "force": True,
        })

    await bridge.list_background_processes(
        connection_id,
        {
            "expected_session_id": 'session:5dyf7lfzp7z22pphjtostkgsjm',
            "expected_host_session_id": 'host:7r7elstdigup6xfbo3hd3d5jfu',
            "cursor": "cursor:page",
            "maximum_items": 7,
        },
    )
    kind, listing = connection.requests[-1]
    assert kind == "list_background_processes"
    assert isinstance(listing, wire.ListBackgroundProcessesRequest)
    assert listing.cursor == "cursor:page"
    assert listing.maximum_items == 7

    await bridge.read_background_process_log(
        connection_id,
        {
            "expected_session_id": 'session:5dyf7lfzp7z22pphjtostkgsjm',
            "expected_host_session_id": 'host:7r7elstdigup6xfbo3hd3d5jfu',
            "process_id": 'process:77t67dg5pb4j4dif5xd3xcidwe',
            "output_cursor": "output:17",
            "max_output_chars": 4096,
        },
    )
    kind, log = connection.requests[-1]
    assert kind == "read_background_process_log"
    assert isinstance(log, wire.ReadBackgroundProcessLogRequest)
    assert log.process_id == 'process:77t67dg5pb4j4dif5xd3xcidwe'
    assert log.output_cursor == "output:17"
    assert log.max_output_chars == 4096

    await bridge.aclose()


def test_u2_renderer_bridge_preserves_typed_prompt_order_and_exact_image_read(
    monkeypatch,
) -> None:
    asyncio.run(_exercise_typed_prompt_and_image_read(monkeypatch))


async def _exercise_typed_prompt_and_image_read(monkeypatch) -> None:
    _RuntimeConnection.next_id = 0
    monkeypatch.setattr(renderer_bridge_module, "BrowserRuntimeConnection", _RuntimeConnection)
    monkeypatch.setattr(
        RendererBridge,
        "_connection_payload",
        staticmethod(
            lambda connection: {
                "connection_id": connection.connection_id,
                "session_id": connection.session_id,
                "connection_generation": connection.generation,
                "role": connection.role,
            }
        ),
    )
    bridge = RendererBridge(
        sessions=cast(LocalSessionController, _Sessions()),
        protocol_server=cast(TerminalKernelProtocolServer, object()),
    )
    payload = await bridge.connect('session:5dyf7lfzp7z22pphjtostkgsjm', browser_instance_id=BROWSER_ONE)
    connection_id = str(payload["connection_id"])
    connection = bridge._connections[connection_id]
    repeated = b"\x89PNG\r\n\x1a\nexact"
    await bridge.command(
        connection_id,
        {
            "command_id": 'command:phj426c6i6nucnncfbxhn4rqki',
            "command_kind": "SUBMIT_PROMPT",
            "requested_permission_mode": "PERMISSION_MODE_READ_ONLY",
            "prompt_content": {
                "parts": [
                    {"type": "text", "text": "before\\n[Figure 1]"},
                    {
                        "type": "image",
                        "content_base64": base64.b64encode(repeated).decode("ascii"),
                        "declared_media_type": "image/png",
                    },
                    {"type": "text", "text": "after"},
                    {
                        "type": "image",
                        "content_base64": base64.b64encode(repeated).decode("ascii"),
                        "declared_media_type": "image/png",
                    },
                ]
            },
        },
    )
    kind, request = connection.requests[-1]
    assert kind == "command"
    assert isinstance(request, wire.CommandRequest)
    assert request.command_kind == wire.SUBMIT_PROMPT
    assert request.target_turn_id == ""
    assert [part.WhichOneof("value") for part in request.prompt_content.parts] == [
        "text",
        "image",
        "text",
        "image",
    ]
    assert request.prompt_content.parts[0].text == "before\\n[Figure 1]"
    assert request.prompt_content.parts[1].image.content == repeated
    assert request.prompt_content.parts[1].image.declared_media_type == "image/png"
    assert request.prompt_content.parts[2].text == "after"
    assert request.prompt_content.parts[3].image.content == repeated

    response = await bridge.read_content(
        connection_id,
        {
            "entry_id": 'entry:fssz5csfdy3rpaorzhjfs5wjqi',
            "image_ref_ordinal": 2,
            "offset_bytes": 0,
            "limit_bytes": 1 << 20,
        },
    )
    kind, image_request = connection.requests[-1]
    assert kind == "read_content"
    assert isinstance(image_request, wire.ReadContentRequest)
    assert image_request.entry_id == 'entry:fssz5csfdy3rpaorzhjfs5wjqi'
    assert image_request.queue_item_id == ""
    assert image_request.HasField("image_ref_ordinal")
    assert image_request.image_ref_ordinal == 2
    assert response["content"]["content"] == base64.b64encode(
        b"exact-image-bytes"
    ).decode("ascii")
    image_ref = "image:" + "a" * 26
    await bridge.read_content(connection_id, {"image_ref": image_ref, "limit_bytes": 256})
    kind, request = connection.requests[-1]
    assert kind == "read_content" and request.WhichOneof("target") == "image_ref"
    assert request.image_ref == image_ref
    for extra in ({"entry_id": "entry:" + "a"*26}, {"image_ref_ordinal": 0}, {"visualization_ordinal": 0}, {"block_id": ""}):
        with pytest.raises(ValueError):
            await bridge.read_content(connection_id, {"image_ref": image_ref, **extra})
    await bridge.aclose()


@pytest.mark.parametrize(
    "body",
    [
        {
            "command_id": 'command:iowksoo3q3cmvr3wbzp5zmdjla',
            "command_kind": "SUBMIT_PROMPT",
            "text": "legacy",
        },
        {
            "command_id": 'command:z73pvorgcpr7lu6vbnkrmuiw2a',
            "command_kind": "SUBMIT_PROMPT",
            "prompt_content": {"parts": [{"type": "text", "text": "no"}]},
            "attachments": [],
        },
    ],
)
def test_u2_renderer_bridge_does_not_restore_removed_prompt_paths(
    monkeypatch, body: dict[str, object]
) -> None:
    asyncio.run(_exercise_removed_prompt_path(monkeypatch, body))


async def _exercise_removed_prompt_path(monkeypatch, body: dict[str, object]) -> None:
    _RuntimeConnection.next_id = 0
    monkeypatch.setattr(renderer_bridge_module, "BrowserRuntimeConnection", _RuntimeConnection)
    monkeypatch.setattr(
        RendererBridge,
        "_connection_payload",
        staticmethod(
            lambda connection: {
                "connection_id": connection.connection_id,
                "session_id": connection.session_id,
                "connection_generation": connection.generation,
                "role": connection.role,
            }
        ),
    )
    bridge = RendererBridge(
        sessions=cast(LocalSessionController, _Sessions()),
        protocol_server=cast(TerminalKernelProtocolServer, object()),
    )
    payload = await bridge.connect('session:5dyf7lfzp7z22pphjtostkgsjm', browser_instance_id=BROWSER_ONE)
    connection_id = str(payload["connection_id"])
    connection = bridge._connections[connection_id]
    with pytest.raises(ValueError):
        await bridge.command(connection_id, body)
    assert connection.requests == []
    await bridge.aclose()


class _FrameWriter:
    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    def write(self, value: bytes) -> None:
        self.written.extend(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


class _FrameReader:
    def __init__(self, response: wire.ServerFrame) -> None:
        payload = response.SerializeToString(deterministic=True)
        self.pending = bytearray(len(payload).to_bytes(4, "big") + payload)

    async def readexactly(self, size: int) -> bytes:
        assert len(self.pending) >= size
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result

    def at_eof(self) -> bool:
        return False


@pytest.mark.parametrize("read_only,target", [(True, 0), (False, 2)])
def test_browser_capacity_uses_authenticated_protocol_transport(read_only, target) -> None:
    async def exercise() -> None:
        response = wire.ServerFrame(subagent_capacity=wire.SubagentCapacityResponse(
            request_id='request:byplvq6cru7c2aicyvuqtymuyu', target=2, occupied=1,
        ))
        writer = _FrameWriter()
        client = TerminalProtocolClient(
            reader=cast(asyncio.StreamReader, _FrameReader(response)),
            writer=cast(asyncio.StreamWriter, writer),
            role=wire.ATTACHMENT_ROLE_CONTROLLER,
            server=cast(TerminalKernelProtocolServer, object()),
            session_id='session:5dyf7lfzp7z22pphjtostkgsjm', host_session_id='host:gubylb5v44ganghpuxoqfvlrji',
        )
        client.attachment_id = 'attachment:p252eyydrofx6fl6pqvmozegvm'
        client.attachment_generation = 3
        returned = await client.request("subagent_capacity", wire.SubagentCapacityRequest(
            request_id='request:byplvq6cru7c2aicyvuqtymuyu', expected_session_id='session:5dyf7lfzp7z22pphjtostkgsjm',
            expected_host_session_id='host:gubylb5v44ganghpuxoqfvlrji', read_only=read_only, target=target,
        ))
        sent = wire.ClientFrame.FromString(bytes(writer.written[4:])).subagent_capacity
        assert sent.attachment_id == 'attachment:p252eyydrofx6fl6pqvmozegvm'
        assert sent.attachment_generation == 3
        assert sent.expected_session_id == 'session:5dyf7lfzp7z22pphjtostkgsjm'
        assert sent.expected_host_session_id == 'host:gubylb5v44ganghpuxoqfvlrji'
        assert sent.read_only == read_only
        assert sent.target == target
        assert returned.subagent_capacity.target == 2
        assert returned.subagent_capacity.occupied == 1

    asyncio.run(exercise())


def test_u2_protocol_frame_counts_complete_typed_prompt_at_eight_mib_boundary() -> None:
    asyncio.run(_exercise_protocol_frame_boundary())


async def _exercise_protocol_frame_boundary() -> None:
    frame = wire.ClientFrame(
        command=wire.CommandRequest(
            request_id='request:ampzufg6vkqsvl2afcdzp4otru',
            attachment_id='attachment:msixoqvujl5evfiu57n63nkhtq',
            attachment_generation=1,
            command_id='command:tnf5j42plo2fbjoalmjki3yowm',
            client_submission_id='command:tnf5j42plo2fbjoalmjki3yowm',
            command_kind=wire.SUBMIT_PROMPT,
            requested_permission_mode=wire.PERMISSION_MODE_READ_ONLY,
            prompt_content=wire.PromptContent(
                parts=[
                    wire.PromptContentPart(text="escaping \\\" 中文\n"),
                    wire.PromptContentPart(
                        image=wire.PromptImagePart(
                            content=b"x" * (MAXIMUM_FRAME_BYTES - 256),
                            declared_media_type="image/png",
                        )
                    ),
                ]
            ),
        )
    )
    current_size = len(frame.SerializeToString(deterministic=True))
    delta = MAXIMUM_FRAME_BYTES - current_size
    assert 0 < delta < 256
    frame.command.prompt_content.parts[1].image.content += b"x" * delta
    assert len(frame.SerializeToString(deterministic=True)) == MAXIMUM_FRAME_BYTES

    response = wire.ServerFrame(
        command_outcome=wire.CommandOutcome(
            request_id='request:ampzufg6vkqsvl2afcdzp4otru',
            command_id='command:tnf5j42plo2fbjoalmjki3yowm',
            status=wire.PENDING,
        )
    )
    writer = _FrameWriter()
    client = TerminalProtocolClient(
        reader=cast(asyncio.StreamReader, _FrameReader(response)),
        writer=cast(asyncio.StreamWriter, writer),
        role=wire.ATTACHMENT_ROLE_CONTROLLER,
        server=cast(TerminalKernelProtocolServer, object()),
        session_id='session:5dyf7lfzp7z22pphjtostkgsjm',
        host_session_id='host:gubylb5v44ganghpuxoqfvlrji',
    )
    returned = await client._round_trip(frame)
    assert returned.command_outcome.command_id == 'command:tnf5j42plo2fbjoalmjki3yowm'
    assert int.from_bytes(writer.written[:4], "big") == MAXIMUM_FRAME_BYTES
    assert len(writer.written) == MAXIMUM_FRAME_BYTES + 4

    frame.command.prompt_content.parts[1].image.content += b"x"
    rejected_writer = _FrameWriter()
    rejected = TerminalProtocolClient(
        reader=cast(asyncio.StreamReader, _FrameReader(response)),
        writer=cast(asyncio.StreamWriter, rejected_writer),
        role=wire.ATTACHMENT_ROLE_CONTROLLER,
        server=cast(TerminalKernelProtocolServer, object()),
        session_id='session:5dyf7lfzp7z22pphjtostkgsjm',
        host_session_id='host:gubylb5v44ganghpuxoqfvlrji',
    )
    with pytest.raises(ProtocolBridgeError) as caught:
        await rejected._round_trip(frame)
    assert caught.value.code == "PROTOCOL_FRAME_OUT_OF_BOUNDS"
    assert rejected_writer.written == b""


class _RuntimeReopenHost:
    def __init__(self, session_id: str, host_session_id: str, *, busy: bool = False):
        self.session_id = session_id
        self.host_session_id = host_session_id
        self.writer_generation = 1
        self.busy = busy
        self.gated = False
        self.committed = False
        self.aborted = False

    async def prepare_safe_runtime_reopen(self):
        if self.busy:
            raise RuntimeError("accepted work remains")
        self.gated = True
        return SimpleNamespace(
            session_id=self.session_id,
            host_session_id=self.host_session_id,
            writer_generation=self.writer_generation,
        )

    async def abort_safe_runtime_reopen(self, _quiescence: object) -> None:
        self.gated = False
        self.aborted = True

    async def commit_safe_runtime_reopen(self, _quiescence: object) -> None:
        assert self.gated
        self.committed = True


class _RuntimeReopenCore:
    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = workspace_root
        self.closed_host_ids: list[str] = []
        self.resume_count = 0

    async def read_resumable_session(self, session_id: str, **_kwargs: object):
        return SimpleNamespace(
            session_id=session_id,
            workspace_kind="project",
            workspace_root=self.workspace_root,
            workspace_label="runtime reopen test",
            memory_domain_id="u_local",
        )

    async def close_session(self, host_session_id: str) -> None:
        self.closed_host_ids.append(host_session_id)

    async def resume_session(self, session_id: str, **_kwargs: object):
        self.resume_count += 1
        return _RuntimeReopenHost(
            session_id,
            derive_id("host", "test-fixture", f"host:resumed:{self.resume_count}"),
        )


class _RuntimeReopenConnection:
    def __init__(self, connection_id: str, session_id: str, *, fail: bool = False):
        self.connection_id = connection_id
        self.session_id = session_id
        self.host_session_id = derive_id("host", "test-fixture", f"host:{session_id}")
        self.fail = fail
        self.role = "controller"
        self.generation = 1
        self.is_open = True
        self.close_attempted = False

    async def aclose(self) -> None:
        self.close_attempted = True
        if self.fail:
            raise RuntimeError("close failed")


def _runtime_reopen_controller(
    tmp_path,
    *,
    old_host: _RuntimeReopenHost | None,
) -> tuple[LocalSessionController, _RuntimeReopenCore]:
    workspace_input = HostWorkspaceInput(
        workspace_kind="project",
        workspace_root=tmp_path,
        display_label="runtime reopen test",
        memory_domain_id="u_local",
        cleanup_workspace_root_on_close=False,
        trust_workspace_mcp_config=False,
    )
    core = _RuntimeReopenCore(str(tmp_path))
    controller = object.__new__(LocalSessionController)
    controller.core = core
    controller.memory_domain_id = workspace_input.memory_domain_id
    controller.trust_workspace_mcp_config = workspace_input.trust_workspace_mcp_config
    controller.permission_policy = cast(object, None)
    controller.active_skill_names = frozenset()
    controller._by_session = {}
    controller._by_host = {}
    controller._operations = {}
    controller._forks = set()
    controller._lock = asyncio.Lock()
    controller._closing = False
    controller._close_task = None
    if old_host is not None:
        handle = HostSessionHandle(cast(object, old_host), workspace_input)
        controller._by_session[old_host.session_id] = handle
        controller._by_host[old_host.host_session_id] = handle
    return controller, core


def test_runtime_reopen_busy_keeps_the_old_host_published(tmp_path) -> None:
    async def scenario() -> None:
        old = _RuntimeReopenHost('session:gsikvbdgycchganur2wnjscrg4', 'host:fnej7unuravnczew6u2aj3yev4', busy=True)
        controller, core = _runtime_reopen_controller(tmp_path, old_host=old)

        with pytest.raises(SessionControlRejected) as caught:
            await controller.prepare_runtime_reopen(old.session_id)

        assert caught.value.public_code == "RUNTIME_REOPEN_BUSY"
        assert controller._by_session[old.session_id].session is old
        assert controller._operations == {}
        assert core.closed_host_ids == []

    asyncio.run(scenario())


def test_runtime_reopen_full_detaches_closes_and_resumes_exactly_once(tmp_path) -> None:
    async def scenario() -> None:
        old = _RuntimeReopenHost('session:744hkamlh7wp6hdanqkno5tvf4', 'host:bdrf3raz6vcwmrrowx6l566uc4')
        controller, core = _runtime_reopen_controller(tmp_path, old_host=old)
        bridge = RendererBridge(
            sessions=controller,
            protocol_server=cast(TerminalKernelProtocolServer, object()),
        )
        first = _RuntimeReopenConnection("connection:one", old.session_id)
        second = _RuntimeReopenConnection("connection:two", old.session_id)
        bridge._connections = {first.connection_id: first, second.connection_id: second}
        bridge._controller_by_session[old.session_id] = first.connection_id

        operation = await controller.prepare_runtime_reopen(old.session_id)
        assert isinstance(operation, PreparedRuntimeReopenOperation)
        assert controller._by_session[old.session_id].session is old
        detach = await bridge.detach_session_for_runtime_reopen(operation)
        assert isinstance(detach, BridgeDetachFull)
        assert first.close_attempted and second.close_attempted
        assert not bridge._connections

        host_outcome = await controller.prepare_runtime_reopen_close(operation)
        assert isinstance(host_outcome, OldHostCloseFull)
        settlement = await bridge.settle_runtime_reopen_detach(
            detach.token, host_outcome
        )
        assert isinstance(settlement, BridgeSettlementFull)
        observation = await controller.finalize_runtime_reopen(
            operation,
            host_outcome=host_outcome,
            bridge_settlement=settlement,
            bridge_owner=bridge,
        )
        resumed = await controller.resume_session(
            old.session_id,
            no_live_observation=observation,
        )

        assert old.committed
        assert core.closed_host_ids == [old.host_session_id]
        assert core.resume_count == 1
        assert resumed.host_session_id == 'host:sfhbohufvlifjsr6tcy7qtdbqa'
        assert controller._by_session[old.session_id] is resumed

    asyncio.run(scenario())


def test_runtime_reopen_without_old_host_uses_distinct_closed_outcome(tmp_path) -> None:
    async def scenario() -> None:
        controller, core = _runtime_reopen_controller(tmp_path, old_host=None)
        bridge = RendererBridge(
            sessions=controller,
            protocol_server=cast(TerminalKernelProtocolServer, object()),
        )

        operation = await controller.prepare_runtime_reopen('session:2iewavcc2san25uuwrwmaxpaai')
        assert isinstance(operation, PreparedRuntimeResumeOperation)
        detach = await bridge.detach_session_for_runtime_reopen(operation)
        assert isinstance(detach, BridgeDetachFull)
        host_outcome = await controller.prepare_runtime_reopen_close(operation)
        assert isinstance(host_outcome, NoOldHostReadyToResume)
        settlement = await bridge.settle_runtime_reopen_detach(
            detach.token, host_outcome
        )
        assert isinstance(settlement, BridgeSettlementFull)
        observation = await controller.finalize_runtime_reopen(
            operation,
            host_outcome=host_outcome,
            bridge_settlement=settlement,
            bridge_owner=bridge,
        )
        resumed = await controller.resume_session(
            operation.session_id,
            no_live_observation=observation,
        )

        assert core.closed_host_ids == []
        assert resumed.host_session_id == 'host:sfhbohufvlifjsr6tcy7qtdbqa'

    asyncio.run(scenario())


def test_runtime_reopen_abort_after_detach_restores_only_the_exact_old_host(
    tmp_path,
) -> None:
    async def scenario() -> None:
        old = _RuntimeReopenHost('session:mjszjoixjoh2z6r4dafvcwo4fq', 'host:6jsxuoe2onmvunsj2ghwlhitcq')
        controller, core = _runtime_reopen_controller(tmp_path, old_host=old)
        bridge = RendererBridge(
            sessions=controller,
            protocol_server=cast(TerminalKernelProtocolServer, object()),
        )
        connection = _RuntimeReopenConnection("connection:abort", old.session_id)
        bridge._connections[connection.connection_id] = connection

        operation = await controller.prepare_runtime_reopen(old.session_id)
        detach = await bridge.detach_session_for_runtime_reopen(operation)
        assert isinstance(detach, BridgeDetachFull)
        await controller.prepare_abort_runtime_reopen(operation)
        settlement = await bridge.settle_runtime_reopen_abort(detach.token)
        assert isinstance(settlement, BridgeSettlementFull)
        await controller.finalize_abort_runtime_reopen(
            operation,
            bridge_settlement=settlement,
            bridge_owner=bridge,
        )

        assert old.aborted and not old.gated and not old.committed
        assert controller._by_session[old.session_id].session is old
        assert core.closed_host_ids == []
        assert controller._operations == {}

    asyncio.run(scenario())


def test_runtime_reopen_connection_close_failure_quarantines_both_owners(
    tmp_path,
) -> None:
    async def scenario() -> None:
        old = _RuntimeReopenHost('session:lhkh7dhxu3ngcpvrrcxv5ald6y', 'host:km6h7pl3f6es74w5k2zr2jxmji')
        controller, core = _runtime_reopen_controller(tmp_path, old_host=old)
        bridge = RendererBridge(
            sessions=controller,
            protocol_server=cast(TerminalKernelProtocolServer, object()),
        )
        failed = _RuntimeReopenConnection(
            "connection:failed", old.session_id, fail=True
        )
        settled = _RuntimeReopenConnection("connection:settled", old.session_id)
        bridge._connections = {
            failed.connection_id: failed,
            settled.connection_id: settled,
        }

        operation = await controller.prepare_runtime_reopen(old.session_id)
        detach = await bridge.detach_session_for_runtime_reopen(operation)
        assert isinstance(detach, BridgeDetachFailed)
        assert failed.close_attempted and settled.close_attempted
        settlement = await bridge.quarantine_detach_failure(
            detach.token,
            error=detach.error,
        )
        assert isinstance(settlement, BridgeSettlementFailed)
        await controller.quarantine_runtime_reopen(
            operation,
            public_code="RUNTIME_REOPEN_QUARANTINED",
        )

        with pytest.raises(SessionControlRejected) as caught:
            await controller.resume_session(old.session_id)
        assert caught.value.public_code == "RUNTIME_REOPEN_QUARANTINED"
        with pytest.raises(RuntimeError, match="quarantined"):
            await bridge.connect(
                old.session_id,
                browser_instance_id=BROWSER_ONE,
            )
        assert core.closed_host_ids == []

    asyncio.run(scenario())


def test_runtime_reopen_post_detach_exception_quarantines_gate_and_operation(
    tmp_path,
) -> None:
    async def scenario() -> None:
        old = _RuntimeReopenHost('session:aw6h3ewxg4gmli6dw5w4oj5gme', 'host:k63qoqsfkoifkumvxmduvecfvq')
        controller, core = _runtime_reopen_controller(tmp_path, old_host=old)
        bridge = RendererBridge(
            sessions=controller,
            protocol_server=cast(TerminalKernelProtocolServer, object()),
        )
        connection = _RuntimeReopenConnection(
            "connection:post-token", old.session_id
        )
        bridge._connections[connection.connection_id] = connection
        bridge._controller_by_session[old.session_id] = connection.connection_id

        async def fail_after_detach(_operation):
            raise RuntimeError("injected post-detach failure")

        controller.prepare_runtime_reopen_close = fail_after_detach  # type: ignore[method-assign]
        static_root = tmp_path / "static"
        static_root.mkdir()
        (static_root / "index.html").write_text("Pulsara", encoding="utf-8")
        runtime = test_model_runtime()

        async def refresh_database_state() -> None:
            return None

        async def unexpected_reset(_postgres):
            raise AssertionError("unexpected reset")

        server = LocalHttpServer(
            sessions=controller,
            bridge=bridge,
            static_root=static_root,
            requested_port=0,
            is_ready=lambda: True,
            is_draining=lambda: False,
            settings=runtime.settings,
            catalog=runtime.catalog,
            model_runtime=runtime,
            database_state=lambda: "ready",
            refresh_database_state=refresh_database_state,
            reset_postgres=unexpected_reset,
        )
        await server.start()
        try:
            async with ClientSession(cookie_jar=DummyCookieJar()) as client:
                async with client.post(
                    f"{server.origin}/api/sessions/{old.session_id}/runtime/reopen",
                    headers={
                        "X-Pulsara-Connection-Id": connection.connection_id,
                        "X-Pulsara-Connection-Generation": "1",
                        "Origin": server.origin,
                        "Sec-Fetch-Site": "same-origin",
                    },
                ) as response:
                    assert response.status == 409
                    payload = await response.json()
                    assert payload["error"]["code"] == "RUNTIME_REOPEN_QUARANTINED"
            assert connection.close_attempted
            with pytest.raises(SessionControlRejected) as caught:
                await controller.resume_session(old.session_id)
            assert caught.value.public_code == "RUNTIME_REOPEN_QUARANTINED"
            with pytest.raises(RuntimeError, match="quarantined"):
                await bridge.connect(
                    old.session_id,
                    browser_instance_id=BROWSER_ONE,
                )
            assert core.closed_host_ids == []
        finally:
            await server.aclose()

    asyncio.run(scenario())


def test_history_http_round_trips_protobuf_cursor_without_losing_cut_or_precision(
    tmp_path,
) -> None:
    """The browser returns the snapshot's protobuf JSON cursor unchanged."""
    async def scenario() -> None:
        session_id = 'session:5dyf7lfzp7z22pphjtostkgsjm'
        read_calls = []

        class Reader:
            def history_page(self, **values):
                read_calls.append(values)
                return wire.HistoryPageResponse()

        state = SimpleNamespace(
            host_session=SimpleNamespace(session_id=session_id),
            protocol_reader=Reader(),
        )
        gateway = SimpleNamespace(
            _maximum_frame_bytes=MAXIMUM_FRAME_BYTES,
            _fits_frame=lambda _frame: True,
        )

        class Connection(_RuntimeConnection):
            async def request(self, kind, request):
                assert kind == "history_page"
                self.requests.append((kind, request))
                return await TerminalKernelProtocolServer._history(gateway, state, request)

        bridge = RendererBridge(
            sessions=cast(LocalSessionController, _Sessions()),
            protocol_server=cast(TerminalKernelProtocolServer, object()),
        )
        connection = Connection(session_id=session_id, generation=1, role="observer")
        bridge._connections[connection.connection_id] = connection
        static_root = tmp_path / "static"
        static_root.mkdir()
        (static_root / "index.html").write_text("Pulsara", encoding="utf-8")
        runtime = test_model_runtime()

        async def refresh_database_state():
            return None

        async def unexpected_reset(_postgres):
            raise AssertionError("unexpected reset")

        server = LocalHttpServer(
            sessions=bridge.sessions, bridge=bridge, static_root=static_root,
            requested_port=0, is_ready=lambda: True, is_draining=lambda: False,
            settings=runtime.settings, catalog=runtime.catalog, model_runtime=runtime,
            database_state=lambda: "ready", refresh_database_state=refresh_database_state,
            reset_postgres=unexpected_reset,
        )
        await server.start()
        try:
            async with ClientSession(cookie_jar=DummyCookieJar()) as client:
                url = f"{server.origin}/api/connections/{connection.connection_id}/history"

                async def post_cursor(cursor):
                    async with client.post(url, json={"cursor": cursor}) as response:
                        return response.status, await response.json()

                # Use the actual JSON serializer, including its uint64 strings.
                for event_cut in (0, 2**53 + 3):
                    cursor = wire.HistoryCursor(
                        session_id=session_id, cut_sequence=2**53 + 1,
                        entry_sequence=2**53, event_sequence_cut=event_cut,
                    )
                    status, frame = await post_cursor(renderer_bridge_module.protobuf_json(cursor))
                    assert status == 200
                    assert "history_page" in frame
                    sent = connection.requests[-1][1].cursor
                    assert sent == cursor
                    assert sent.HasField("event_sequence_cut")
                    assert read_calls[-1]["event_sequence_cut"] == event_cut
                    assert read_calls[-1]["cut_sequence"] == 2**53 + 1
                    assert read_calls[-1]["before_entry_sequence"] == 2**53

                # Missing/null optional values must not silently become an accepted zero cut.
                base = {"session_id": session_id, "cut_sequence": "5", "entry_sequence": "4"}
                for cursor in (base, {**base, "event_sequence_cut": None}):
                    status, frame = await post_cursor(cursor)
                    assert status == 200
                    assert frame["error"]["stable_code"] == "HISTORY_CURSOR_INVALID"
                    assert not connection.requests[-1][1].cursor.HasField("event_sequence_cut")
                assert len(read_calls) == 2

                for invalid in (True, "-1", str(2**64), "1.5"):
                    status, frame = await post_cursor({**base, "event_sequence_cut": invalid})
                    assert status == 400
                    assert frame["error"]["code"] == "BAD_REQUEST"
                status, frame = await post_cursor({**base, "event_sequence_cut": "5", "unknown": "x"})
                assert status == 400
                assert frame["error"]["code"] == "BAD_REQUEST"
                assert len(connection.requests) == 4
        finally:
            await server.aclose()
            await bridge.aclose()

    asyncio.run(scenario())

@pytest.mark.parametrize('sequence', ['0', str(2**53 + 3), str(2**64 - 1)])
def test_browser_observe_accepts_exact_decimal_wire_cursor(sequence):
    from unittest.mock import AsyncMock

    async def exercise():
        bridge = object.__new__(RendererBridge)
        observer = SimpleNamespace(request=AsyncMock(return_value=wire.ServerFrame()))
        bridge._connection = AsyncMock(return_value=SimpleNamespace(observer=observer))
        await bridge.observe('browser', {'after_event_sequence': sequence})
        kind, request = observer.request.await_args.args
        assert kind == 'observe'
        assert request.after_event_sequence == int(sequence)
        assert request.maximum_events == 128
    asyncio.run(exercise())


@pytest.mark.parametrize("cancel_first_waiter", [False, True])
def test_lost_writer_resume_joins_old_close_once_before_cold_open(tmp_path, cancel_first_waiter):
    async def scenario():
        old = _RuntimeReopenHost('session:gsikvbdgycchganur2wnjscrg4', 'host:fnej7unuravnczew6u2aj3yev4')
        old.writer_unavailable = True
        controller, core = _runtime_reopen_controller(tmp_path, old_host=old)
        entered, release = asyncio.Event(), asyncio.Event()

        async def close(host_id):
            core.closed_host_ids.append(host_id)
            entered.set()
            await release.wait()
        core.close_session = close
        first = asyncio.create_task(controller.resume_session(old.session_id))
        await entered.wait()
        second = asyncio.create_task(controller.resume_session(old.session_id))
        await asyncio.sleep(0)
        assert core.resume_count == 0
        assert old.session_id in controller._operations
        if cancel_first_waiter:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        release.set()
        resumed = await second
        if not cancel_first_waiter:
            assert await first is resumed
        assert core.closed_host_ids == [old.host_session_id]
        assert core.resume_count == 1
        assert controller._by_session[old.session_id] is resumed
        assert old.host_session_id not in controller._by_host
        assert old.session_id not in controller._operations
    asyncio.run(scenario())


def test_lost_writer_physical_close_failure_keeps_original_quarantine(tmp_path):
    async def scenario():
        old = _RuntimeReopenHost('session:gsikvbdgycchganur2wnjscrg4', 'host:fnej7unuravnczew6u2aj3yev4')
        old.writer_unavailable = True
        controller, core = _runtime_reopen_controller(tmp_path, old_host=old)
        failure = OSError('physical scope join failed with synthetic-secret-value')
        async def close(host_id):
            core.closed_host_ids.append(host_id)
            raise failure
        core.close_session = close
        for _ in range(2):
            with pytest.raises(SessionControlRejected) as caught:
                await controller.resume_session(old.session_id)
            assert caught.value.public_code == 'SESSION_RECOVERY_CLOSE_FAILED'
            assert caught.value.__cause__ is failure
            assert 'synthetic-secret-value' not in str(caught.value)
            assert 'synthetic-secret-value' in str(caught.value.__cause__)
        assert core.resume_count == 0
        assert core.closed_host_ids == [old.host_session_id]
        assert controller._operations[old.session_id].cause is failure
    asyncio.run(scenario())


def test_writer_recovery_does_not_cold_open_after_controller_close_started(tmp_path):
    from pulsara_agent.conversation_kernel.host import KernelHostCoreClosing

    async def scenario():
        old = _RuntimeReopenHost('session:gsikvbdgycchganur2wnjscrg4', 'host:fnej7unuravnczew6u2aj3yev4')
        old.writer_unavailable = True
        controller, core = _runtime_reopen_controller(tmp_path, old_host=old)
        entered, release = asyncio.Event(), asyncio.Event()
        async def close(host_id):
            core.closed_host_ids.append(host_id)
            entered.set()
            await release.wait()
        core.close_session = close
        resuming = asyncio.create_task(controller.resume_session(old.session_id))
        await entered.wait()
        closing = asyncio.create_task(controller.aclose())
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(KernelHostCoreClosing):
            await resuming
        await closing
        assert core.closed_host_ids == [old.host_session_id]
        assert core.resume_count == 0
        assert controller._by_session == controller._by_host == controller._operations == {}
    asyncio.run(scenario())


def test_browser_lost_writer_interrupts_observation_without_cold_open(tmp_path):
    async def scenario():
        old = _RuntimeReopenHost('session:gsikvbdgycchganur2wnjscrg4', 'host:fnej7unuravnczew6u2aj3yev4')
        old.writer_unavailable = True
        controller, core = _runtime_reopen_controller(tmp_path, old_host=old)
        bridge = RendererBridge(sessions=controller, protocol_server=cast(object, None))
        connection = SimpleNamespace(connection_id='browser', session_id=old.session_id,
            host_session_id=old.host_session_id, is_open=True)
        bridge._connections['browser'] = connection
        with pytest.raises(ProtocolBridgeError) as caught:
            await bridge.observe('browser', {})
        assert caught.value.code == 'SESSION_WRITER_UNAVAILABLE'
        assert core.closed_host_ids == [] and core.resume_count == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('scrub_available', [True, False])
def test_recovery_http_details_use_existing_credential_scrub_boundary(scrub_available):
    from unittest.mock import AsyncMock
    from pulsara_agent.desktop_app.http_server import HttpPublicError
    from pulsara_agent.process_credential_boundary import ProcessCredentialScrubSet

    async def scenario():
        secret = 'synthetic-secret-value'
        cause = OSError(f'physical scope join failed: {secret}')
        rejection = SessionControlRejected('SESSION_RECOVERY_CLOSE_FAILED', '连接恢复未完成。')
        rejection.__cause__ = cause
        scrub = ProcessCredentialScrubSet()
        scrub.observe(secret)
        server = object.__new__(LocalHttpServer)
        server._json_body = AsyncMock(return_value={'browser_instance_id': derive_id('transport', 'test-fixture', 'browser')})
        server.bridge = SimpleNamespace(connect=AsyncMock(side_effect=rejection))
        server.sessions = SimpleNamespace(_capture_capability_notice_scrub=AsyncMock(return_value=scrub if scrub_available else None))
        with pytest.raises(HttpPublicError) as caught:
            await server._connect(SimpleNamespace(match_info={'session_id':'session:gsikvbdgycchganur2wnjscrg4'}))
        assert secret not in str(caught.value)
        assert caught.value.retryable is False
        assert ('physical scope join failed' in str(caught.value)) is scrub_available
        assert caught.value.__cause__ is rejection
        assert rejection.__cause__ is cause
    asyncio.run(scenario())
